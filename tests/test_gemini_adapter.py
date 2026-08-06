"""
Tests for app/services/llm_adapters/gemini_adapter.py.

Regression coverage for the actual bug report: Gemini (specifically
enterprise Vertex AI) never got tools injected and couldn't invoke them,
because the app had zero Gemini-aware code — every non-Ollama/non-Anthropic
provider silently routed through OpenAIAdapter, which can't express Vertex's
mandatory OAuth2 service-account auth and rides Gemini's admittedly
"not pixel-perfect" OpenAI-compatibility shim for tool calling.

Also covers three bugs an independent Opus review caught in the first
version of this adapter, all verified against the real installed SDK before
fixing:
  1. Content(role="tool") — not a valid Gemini role (only "user"/"model").
     The model successfully emits a function_call; feeding the result back
     400s. Tools looked "not invoked" when the real failure was one turn
     later.
  2. Parallel tool calls (chat rooms can emit >1 per turn) produced one
     Content per tool result instead of coalescing them into a single
     Content — Gemini requires the response turn to match the call turn's
     part count in ONE Content.
  3. Client-side schema sanitization (stripping additionalProperties/$ref/
     oneOf/anyOf/allOf) was actively wrong: FunctionDeclaration.parameters_
     json_schema's own SDK docstring example includes
     "additionalProperties": false, and stripping anyOf/$ref empirically
     turned valid optional/union schemas into broken ones.

All google.genai calls are mocked — no real GCP credentials or network
calls are exercised here.
"""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from google.auth import exceptions as google_auth_exceptions
from google.genai import errors, types

from app.services.llm import TransientError
from app.services.llm_adapters import gemini_adapter as gemini_adapter_mod
from app.services.llm_adapters.gemini_adapter import (
    GeminiAdapter,
    _to_gemini_contents,
    _to_gemini_tools,
)


FAKE_SA_JSON = json.dumps({
    "type": "service_account",
    "project_id": "my-proj",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "client_email": "x@my-proj.iam.gserviceaccount.com",
})


def _fake_candidate(finish_reason="STOP"):
    c = MagicMock()
    c.finish_reason = finish_reason
    return c


def _fake_response(text="", function_calls=None, finish_reason="STOP",
                    candidates=True, prompt_tokens=100, output_tokens=50, total_tokens=None):
    """A MagicMock shaped like a real GenerateContentResponse, with sane
    defaults — real fields set explicitly rather than left as auto-mocks,
    since an unconfigured MagicMock attribute is truthy/subscriptable in
    ways that can make a test pass for the wrong reason (e.g. `not
    resp.candidates` being False by accident, not because candidates were
    actually populated)."""
    resp = MagicMock()
    resp.text = text
    resp.function_calls = function_calls or []
    resp.candidates = [_fake_candidate(finish_reason)] if candidates else []
    resp.prompt_feedback = None
    usage = MagicMock()
    usage.prompt_token_count = prompt_tokens
    usage.candidates_token_count = output_tokens
    usage.total_token_count = total_tokens
    resp.usage_metadata = usage
    return resp


def _adapter(api_key="AIzaFAKE", model="gemini-2.0-flash"):
    return GeminiAdapter("https://generativelanguage.googleapis.com", api_key, model)


# ── Tool schema translation ──────────────────────────────────────────────────

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

    def test_schema_is_passed_through_unmodified_including_additional_properties(self):
        """Regression: an earlier version stripped additionalProperties/
        $ref/oneOf/anyOf/allOf on the theory Gemini's schema is a strict
        OpenAPI 3.0 subset that rejects them. Verified against the
        installed SDK that FunctionDeclaration.parameters_json_schema's
        own docstring example INCLUDES additionalProperties: false — the
        field exists specifically to accept standard JSON Schema. Stripping
        empirically corrupted valid schemas (anyOf: [string, null] — an
        optional string — became a bare untyped {}), so nothing should be
        removed client-side any more."""
        schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "flags": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "additionalProperties": False,
            "$ref": "#/$defs/Args",
        }
        tool_defs = [{"type": "function", "function": {"name": "f", "description": "d", "parameters": schema}}]
        decl = _to_gemini_tools(tool_defs)[0].function_declarations[0]
        assert decl.parameters_json_schema == schema


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

    def test_tool_result_uses_role_user_not_tool(self):
        """Regression: Gemini's Content.role accepts only "user"/"model" —
        confirmed against the field description AND the SDK's own
        automatic-function-calling loop, which always builds the
        function-response turn as role="user". An earlier version used
        role="tool", which constructs fine (pydantic doesn't validate the
        enum) but 400s at the API on the second turn of any tool call."""
        messages = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                              "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
        ]
        _, contents = _to_gemini_contents(messages)
        tool_content = contents[1]
        assert tool_content.role == "user"
        fr_part = tool_content.parts[0]
        assert fr_part.function_response.name == "read_file"
        assert fr_part.function_response.response == {"result": "file contents"}

    def test_parallel_tool_results_coalesced_into_one_content(self):
        """Regression: a single assistant turn with N parallel tool_calls
        followed by N separate OpenAI tool-role messages used to become N
        separate Gemini Contents — one per result. Gemini requires the
        response turn for a parallel call to be a SINGLE Content whose part
        count matches the call turn. This only manifests with >1 tool call
        per turn (chat rooms), which is exactly the surface this adapter
        targets — the Task executor's strictly-serial one-call-per-turn
        pattern never happened to trigger it."""
        messages = [
            {"role": "assistant", "content": None,
             "tool_calls": [
                 {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                 {"id": "call_2", "type": "function", "function": {"name": "list_directory", "arguments": "{}"}},
             ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file A contents"},
            {"role": "tool", "tool_call_id": "call_2", "content": "dir listing"},
        ]
        _, contents = _to_gemini_contents(messages)
        assert len(contents) == 2  # one model (call) turn, one user (response) turn — not 3
        call_turn, response_turn = contents
        assert call_turn.role == "model"
        assert len(call_turn.parts) == 2
        assert response_turn.role == "user"
        assert len(response_turn.parts) == 2
        names = {p.function_response.name for p in response_turn.parts}
        assert names == {"read_file", "list_directory"}

    def test_tool_results_flushed_before_a_following_user_message(self):
        """The coalescing buffer must flush on ANY non-tool message, not
        just the next assistant tool_calls turn — otherwise a tool result
        followed by a plain user message would either get lost or merged
        into the wrong turn."""
        messages = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                              "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
            {"role": "user", "content": "thanks, now do something else"},
        ]
        _, contents = _to_gemini_contents(messages)
        assert len(contents) == 3
        assert contents[1].role == "user" and contents[1].parts[0].function_response is not None
        assert contents[2].role == "user" and contents[2].parts[0].text == "thanks, now do something else"

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
    def setup_method(self):
        gemini_adapter_mod._client_cache.clear()

    def teardown_method(self):
        gemini_adapter_mod._client_cache.clear()

    def test_vertex_mode_when_api_key_is_service_account_json(self):
        adapter = GeminiAdapter("https://us-central1-aiplatform.googleapis.com/...", FAKE_SA_JSON,
                                 "gemini-2.5-pro", extra={"project_id": "my-proj", "location": "us-central1"})
        with patch("google.oauth2.service_account.Credentials.from_service_account_info") as mock_creds, \
             patch("google.genai.Client") as mock_client:
            mock_creds.return_value = "fake-credentials"
            adapter._build_client()
            mock_creds.assert_called_once()
            assert mock_creds.call_args.args[0]["project_id"] == "my-proj"
            _, kwargs = mock_client.call_args
            assert kwargs["vertexai"] is True
            assert kwargs["project"] == "my-proj"
            assert kwargs["location"] == "us-central1"
            assert kwargs["credentials"] == "fake-credentials"

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
            _, kwargs = mock_client.call_args
            assert kwargs["api_key"] == "AIzaFAKEKEY"

    def test_default_location_is_us_central1(self):
        adapter = GeminiAdapter("https://...", FAKE_SA_JSON, "gemini-2.5-pro", extra={"project_id": "p"})
        assert adapter.location == "us-central1"

    def test_client_is_cached_across_calls_with_same_config(self):
        """Regression: a fresh GeminiAdapter is constructed on every single
        LLM call (dispatcher.get_adapter), so without process-lifetime
        caching, every call would mint a new service-account Credentials
        object and do a fresh OAuth token exchange — defeating the whole
        point of Credentials objects, which are meant to be built once and
        refresh their own token internally."""
        with patch("google.genai.Client") as mock_client:
            mock_client.return_value = MagicMock()
            a1 = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
            a2 = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
            c1 = a1._build_client()
            c2 = a2._build_client()
            assert c1 is c2
            mock_client.assert_called_once()

    def test_client_cache_misses_on_different_credentials(self):
        with patch("google.genai.Client") as mock_client:
            mock_client.return_value = MagicMock()
            a1 = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE_1", "gemini-2.0-flash")
            a2 = GeminiAdapter("https://generativelanguage.googleapis.com", "AIzaFAKE_2", "gemini-2.0-flash")
            a1._build_client()
            a2._build_client()
            assert mock_client.call_count == 2

    def test_timeout_configured_from_config(self):
        with patch("google.genai.Client") as mock_client:
            _adapter()._build_client()
            _, kwargs = mock_client.call_args
            assert kwargs["http_options"].timeout == 600_000  # Config.LLM_TIMEOUT default, in ms


# ── complete() — response parsing and error mapping ──────────────────────────

class TestComplete:
    def setup_method(self):
        gemini_adapter_mod._client_cache.clear()

    def teardown_method(self):
        gemini_adapter_mod._client_cache.clear()

    def test_success_returns_text_tool_calls_and_tokens(self):
        adapter = _adapter()
        fake_fc = MagicMock(id="fc1", args={"path": "a.txt"})
        fake_fc.name = "read_file"  # MagicMock(name=...) sets the mock's own repr name, not an attribute
        fake_resp = _fake_response(text="Here's the file", function_calls=[fake_fc])

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
        assert tokens == 150  # no total_token_count on the fixture -> falls back to input+output
        assert adapter.last_usage == {"input": 100, "output": 50, "model": "gemini-2.0-flash"}

    def test_tools_and_tool_config_actually_reach_generate_content(self):
        """The one behavior this adapter exists to deliver — tools= and
        tool_config= present on the outbound request when tool_defs are
        given. Every other test mocks the response; this is the only one
        that inspects what was actually SENT."""
        adapter = _adapter()
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = _fake_response(text="ok")

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            adapter.complete(
                [{"role": "user", "content": "hi"}],
                [{"type": "function", "function": {"name": "read_file", "description": "d",
                                                     "parameters": {"type": "object"}}}],
            )

        _, kwargs = fake_client.models.generate_content.call_args
        config = kwargs["config"]
        assert config.tools is not None and len(config.tools) == 1
        assert config.tools[0].function_declarations[0].name == "read_file"
        assert config.tool_config.function_calling_config.mode == "AUTO"

    def test_no_tools_omits_tools_and_tool_config(self):
        adapter = _adapter()
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = _fake_response(text="ok")

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            adapter.complete([{"role": "user", "content": "hi"}], [])

        _, kwargs = fake_client.models.generate_content.call_args
        assert kwargs["config"] is None

    def test_no_tool_calls_returns_empty_list(self):
        adapter = _adapter()
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = _fake_response(text="just text")

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            text, tool_calls, _ = adapter.complete([{"role": "user", "content": "hi"}], [])
        assert tool_calls == []
        assert text == "just text"

    def test_function_call_only_response_has_empty_text_not_an_error(self):
        """.text is Optional[str] — None (not a raise) for a response with
        only function_call parts, confirmed against the installed SDK's
        GenerateContentResponse._get_text(). A pure tool-call turn is the
        normal shape of a response, not an error condition."""
        adapter = _adapter()
        fake_resp = _fake_response(text=None, function_calls=[])
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            text, _, _ = adapter.complete([{"role": "user", "content": "hi"}], [])
        assert text == ""

    def test_uses_total_token_count_when_available(self):
        """Regression: input+output (prompt_token_count + candidates_token_
        count) omits thoughts_token_count, which IS billed on thinking
        models (gemini-2.5-*, the models this adapter targets) and IS what
        _check_token_budget enforces against via the returned sum —
        undercounting here means the budget silently stops binding on
        exactly the models this adapter is for."""
        adapter = _adapter()
        fake_resp = _fake_response(text="ok", prompt_tokens=100, output_tokens=50, total_tokens=500)
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            _, _, tokens = adapter.complete([{"role": "user", "content": "hi"}], [])
        assert tokens == 500  # not 150 (input+output) — includes thinking tokens

    def test_blocked_prompt_raises_clear_error_not_silent_empty_success(self):
        """Regression: without checking prompt_feedback/finish_reason, a
        blocked call comes back as empty text + no tool_calls —
        indistinguishable from a legitimate empty reply. The Task executor
        treats "no tool_calls" as a successfully completed run, so a
        content-policy block was silently recorded as task success."""
        adapter = _adapter()
        fake_resp = _fake_response(candidates=False)
        fake_resp.prompt_feedback = MagicMock(block_reason="SAFETY")
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(RuntimeError, match="blocked the prompt"):
                adapter.complete([{"role": "user", "content": "hi"}], [])

    def test_safety_blocked_response_raises_clear_error(self):
        adapter = _adapter()
        fake_resp = _fake_response(text=None, finish_reason="SAFETY")
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(RuntimeError, match="blocked the response"):
                adapter.complete([{"role": "user", "content": "hi"}], [])

    def test_stop_finish_reason_is_not_treated_as_blocked(self):
        adapter = _adapter()
        fake_resp = _fake_response(text="all good", finish_reason="STOP")
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            text, _, _ = adapter.complete([{"role": "user", "content": "hi"}], [])
        assert text == "all good"

    def test_max_tokens_finish_reason_is_not_treated_as_blocked(self):
        """A truncated-but-legitimate partial reply must still be usable,
        unlike an actual safety/policy block."""
        adapter = _adapter()
        fake_resp = _fake_response(text="partial reply...", finish_reason="MAX_TOKENS")
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_resp

        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            text, _, _ = adapter.complete([{"role": "user", "content": "hi"}], [])
        assert text == "partial reply..."

    def test_server_error_raises_transient(self):
        adapter = _adapter()
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = errors.ServerError(503, {"error": {"message": "down"}}, None)
        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(TransientError):
                adapter.complete([{"role": "user", "content": "hi"}], [])

    def test_rate_limit_client_error_raises_transient(self):
        adapter = _adapter()
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = errors.ClientError(
            429, {"error": {"message": "rate limited"}}, None)
        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(TransientError):
                adapter.complete([{"role": "user", "content": "hi"}], [])

    def test_bad_request_client_error_raises_runtime_error_not_transient(self):
        adapter = _adapter()
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = errors.ClientError(
            400, {"error": {"message": "bad schema"}}, None)
        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(RuntimeError) as exc_info:
                adapter.complete([{"role": "user", "content": "hi"}], [])
        assert not isinstance(exc_info.value, TransientError)

    def test_connection_error_raises_transient(self):
        """Regression: httpx connection errors aren't google.genai.errors.
        APIError subclasses, so without explicit mapping they'd propagate
        uncaught and retry_with_recovery (which only retries RecoveryError
        subclasses) would hard-fail the run on a transient network blip."""
        adapter = _adapter()
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = httpx.ConnectError("connection refused")
        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(TransientError):
                adapter.complete([{"role": "user", "content": "hi"}], [])

    def test_oauth_refresh_error_raises_transient(self):
        """Vertex mode adds an OAuth token-refresh round trip the other
        providers don't have — a blip there must be retryable too, not a
        hard failure."""
        adapter = _adapter()
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = google_auth_exceptions.RefreshError("token refresh failed")
        with patch.object(GeminiAdapter, "_build_client", return_value=fake_client):
            with pytest.raises(TransientError):
                adapter.complete([{"role": "user", "content": "hi"}], [])
