"""
Tests for app/services/llm_adapters/gemini_adapter.py.

Regression coverage for the actual bug report: Gemini (specifically
enterprise Vertex AI) never got tools injected and couldn't invoke them,
because the app had zero Gemini-aware code — every non-Ollama/non-Anthropic
provider silently routed through OpenAIAdapter, which can't express Vertex's
mandatory OAuth2 service-account auth and rides Gemini's admittedly
"not pixel-perfect" OpenAI-compatibility shim for tool calling.

All google.genai calls are mocked — no real GCP credentials or network
calls are exercised here.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from google.genai import errors, types

from app.services.llm import TransientError
from app.services.llm_adapters.gemini_adapter import (
    GeminiAdapter,
    _sanitize_schema_for_gemini,
    _to_gemini_contents,
    _to_gemini_tools,
)


FAKE_SA_JSON = json.dumps({
    "type": "service_account",
    "project_id": "my-proj",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "client_email": "x@my-proj.iam.gserviceaccount.com",
})


# ── Schema sanitization ──────────────────────────────────────────────────────

class TestSanitizeSchema:
    def test_strips_additional_properties(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}
        out = _sanitize_schema_for_gemini(schema)
        assert "additionalProperties" not in out

    def test_strips_ref_and_defs(self):
        schema = {"type": "object", "$ref": "#/$defs/Foo", "$defs": {"Foo": {}}}
        out = _sanitize_schema_for_gemini(schema)
        assert "$ref" not in out
        assert "$defs" not in out

    def test_flattens_anyof_to_first_branch(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
        out = _sanitize_schema_for_gemini(schema)
        assert out == {"type": "string"}

    def test_recurses_into_nested_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "nested": {"type": "object", "properties": {"x": {"type": "string"}},
                           "additionalProperties": True},
            },
        }
        out = _sanitize_schema_for_gemini(schema)
        assert "additionalProperties" not in out["properties"]["nested"]

    def test_leaves_clean_schema_untouched(self):
        schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        assert _sanitize_schema_for_gemini(schema) == schema


class TestToGeminiTools:
    def test_builds_function_declarations(self):
        tool_defs = [{
            "type": "function",
            "function": {"name": "read_file", "description": "Read a file",
                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
        }]
        tools = _to_gemini_tools(tool_defs)
        assert len(tools) == 1
        decl = tools[0].function_declarations[0]
        assert decl.name == "read_file"
        assert decl.description == "Read a file"
        assert decl.parameters_json_schema == {"type": "object", "properties": {"path": {"type": "string"}}}

    def test_empty_tool_defs_returns_empty_list(self):
        assert _to_gemini_tools([]) == []

    def test_sanitizes_bad_schema_constructs(self):
        tool_defs = [{
            "type": "function",
            "function": {"name": "f", "description": "d",
                         "parameters": {"type": "object", "additionalProperties": False}},
        }]
        decl = _to_gemini_tools(tool_defs)[0].function_declarations[0]
        assert "additionalProperties" not in decl.parameters_json_schema


# ── Message translation ──────────────────────────────────────────────────────

class TestToGeminiContents:
    def test_system_messages_collected_separately(self):
        system, contents = _to_gemini_contents([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ])
        assert system == "You are helpful."
        assert len(contents) == 1
        assert contents[0].role == "user"

    def test_multiple_system_messages_joined(self):
        system, _ = _to_gemini_contents([
            {"role": "system", "content": "A"},
            {"role": "system", "content": "B"},
        ])
        assert system == "A\n\nB"

    def test_user_message_becomes_user_content(self):
        _, contents = _to_gemini_contents([{"role": "user", "content": "hello"}])
        assert contents[0].role == "user"
        assert contents[0].parts[0].text == "hello"

    def test_assistant_tool_calls_become_model_content_with_function_call_parts(self):
        messages = [{
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}],
        }]
        _, contents = _to_gemini_contents(messages)
        assert contents[0].role == "model"
        fc_part = contents[0].parts[0]
        assert fc_part.function_call.name == "read_file"
        assert fc_part.function_call.args == {"path": "a.txt"}

    def test_tool_result_uses_name_from_preceding_assistant_call(self):
        """OpenAI tool-result messages only carry tool_call_id, not the
        function name — Gemini's function_response part needs the name, so
        it must be recovered from the assistant turn that made the call."""
        messages = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                              "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
        ]
        _, contents = _to_gemini_contents(messages)
        tool_content = contents[1]
        assert tool_content.role == "tool"
        fr_part = tool_content.parts[0]
        assert fr_part.function_response.name == "read_file"
        assert fr_part.function_response.response == {"result": "file contents"}

    def test_assistant_plain_text_becomes_model_content(self):
        _, contents = _to_gemini_contents([{"role": "assistant", "content": "sure thing"}])
        assert contents[0].role == "model"
        assert contents[0].parts[0].text == "sure thing"

    def test_malformed_tool_call_arguments_do_not_raise(self):
        messages = [{
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": "not json"}}],
        }]
        _, contents = _to_gemini_contents(messages)
        assert contents[0].parts[0].function_call.args == {}


# ── Client construction ──────────────────────────────────────────────────────

class TestBuildClient:
    def test_vertex_mode_when_api_key_is_service_account_json(self):
        adapter = GeminiAdapter("https://us-central1-aiplatform.googleapis.com/...", FAKE_SA_JSON,
                                 "gemini-2.5-pro", extra={"project_id": "my-proj", "location": "us-central1"})
        with patch("google.oauth2.service_account.Credentials.from_service_account_info") as mock_creds, \
             patch("google.genai.Client") as mock_client:
            mock_creds.return_value = "fake-credentials"
            adapter._build_client()
            mock_creds.assert_called_once()
            assert mock_creds.call_args.args[0]["project_id"] == "my-proj"
            mock_client.assert_called_once_with(
                vertexai=True, project="my-proj", location="us-central1",
                credentials="fake-credentials",
            )

    def test_vertex_mode_without_project_id_raises_clear_error(self):
        adapter = GeminiAdapter("https://...aiplatform.googleapis.com/...", FAKE_SA_JSON, "gemini-2.5-pro")
        with pytest.raises(RuntimeError, match="Project ID"):
            adapter._build_client()

    def test_invalid_json_credential_raises_clear_error(self):
        adapter = GeminiAdapter("https://...aiplatform.googleapis.com/...", "{not valid json",
                                 "gemini-2.5-pro", extra={"project_id": "p"})
        with pytest.raises(RuntimeError, match="not valid JSON"):
            adapter._build_client()

    def test_ai_studio_mode_when_api_key_is_plain_string(self):
        adapter = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKEKEY", "gemini-2.0-flash")
        with patch("google.genai.Client") as mock_client:
            adapter._build_client()
            mock_client.assert_called_once_with(api_key="AIzaFAKEKEY")

    def test_default_location_is_us_central1(self):
        adapter = GeminiAdapter("https://...", FAKE_SA_JSON, "gemini-2.5-pro", extra={"project_id": "p"})
        assert adapter.location == "us-central1"


# ── complete() — response parsing and error mapping ──────────────────────────

def _fake_usage(prompt=100, candidates=50):
    usage = MagicMock()
    usage.prompt_token_count = prompt
    usage.candidates_token_count = candidates
    return usage


class TestComplete:
    def test_success_returns_text_tool_calls_and_tokens(self):
        adapter = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")

        fake_fc = MagicMock(id="fc1", args={"path": "a.txt"})
        fake_fc.name = "read_file"  # MagicMock(name=...) sets the mock's own repr name, not an attribute
        fake_resp = MagicMock()
        fake_resp.text = "Here's the file"
        fake_resp.function_calls = [fake_fc]
        fake_resp.usage_metadata = _fake_usage(100, 50)

        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            text, tool_calls, tokens = adapter.complete(
                [{"role": "user", "content": "read a.txt"}],
                [{"type": "function", "function": {"name": "read_file", "description": "d",
                                                     "parameters": {"type": "object"}}}],
            )

        assert text == "Here's the file"
        assert tool_calls == [{"id": "fc1", "name": "read_file", "args": {"path": "a.txt"}}]
        assert tokens == 150
        assert adapter.last_usage == {"input": 100, "output": 50, "model": "gemini-2.0-flash"}

    def test_no_tool_calls_returns_empty_list(self):
        adapter = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
        fake_resp = MagicMock()
        fake_resp.text = "just text"
        fake_resp.function_calls = []
        fake_resp.usage_metadata = _fake_usage()
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            text, tool_calls, _ = adapter.complete([{"role": "user", "content": "hi"}], [])
        assert tool_calls == []
        assert text == "just text"

    def test_text_accessor_raising_falls_back_to_empty_string(self):
        """response.text raises when the response has only function_call
        parts and no text parts — a pure tool-call turn, not an error."""
        adapter = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
        fake_resp = MagicMock()
        type(fake_resp).text = property(lambda self: (_ for _ in ()).throw(ValueError("no text parts")))
        fake_resp.function_calls = []
        fake_resp.usage_metadata = _fake_usage()
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            text, _, _ = adapter.complete([{"role": "user", "content": "hi"}], [])
        assert text == ""

    def test_server_error_raises_transient(self):
        adapter = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = errors.ServerError(503, {"error": {"message": "down"}}, None)
        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(TransientError):
                adapter.complete([{"role": "user", "content": "hi"}], [])

    def test_rate_limit_client_error_raises_transient(self):
        adapter = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = errors.ClientError(
            429, {"error": {"message": "rate limited"}}, None)
        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(TransientError):
                adapter.complete([{"role": "user", "content": "hi"}], [])

    def test_bad_request_client_error_raises_runtime_error_not_transient(self):
        adapter = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = errors.ClientError(
            400, {"error": {"message": "bad schema"}}, None)
        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(RuntimeError) as exc_info:
                adapter.complete([{"role": "user", "content": "hi"}], [])
        assert not isinstance(exc_info.value, TransientError)
