"""
Gemini adapter — Vertex AI (enterprise) via service-account credentials, or
the Gemini Developer/AI Studio API via a plain API key. Uses Google's unified
`google-genai` SDK's native request/response shapes rather than routing
through Gemini's OpenAI-compatibility shim.

Why not the OpenAI-compat shim (the path every other provider in this app
uses via OpenAIAdapter): real enterprise Vertex requires short-lived OAuth2
tokens refreshed roughly hourly, which the shim's simple Bearer-key auth
can't express at all. And even with auth solved, Google's own docs call the
shim "not a pixel-perfect clone" of OpenAI — tool_choice mapping, model-name
prefixing (Vertex wants `publishers/google/models/...`-style vs AI Studio's
bare names), and multi-turn `thought_signature` propagation are all
documented gaps specifically around function/tool calling, which is exactly
what broke for us. The native SDK sidesteps all of that.

Which mode is picked is driven entirely by the shape of `api_key`:
- Starts with "{" → treated as a service-account JSON blob → Vertex mode,
  using `google.oauth2.service_account.Credentials`. Requires `project_id`
  (via `extra`); `location` defaults to us-central1.
- Otherwise → treated as a plain Gemini Developer API key → AI Studio mode.
"""
from __future__ import annotations

import json
import logging
import uuid

from app.services.llm_adapters.base import LLMAdapter
from app.services.llm import TransientError

log = logging.getLogger("orions-belt.adapters.gemini")

_DEFAULT_LOCATION = "us-central1"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _sanitize_schema_for_gemini(schema):
    """Strip/flatten JSON-Schema constructs Gemini's function-calling schema
    (an OpenAPI 3.0 subset) doesn't support. `additionalProperties` is the
    single most commonly reported real-world cause of Gemini silently
    rejecting or ignoring a tool; `$ref`/`$defs`/`oneOf`/`anyOf`/`allOf`
    aren't part of that subset either. Our own builtin tool schemas don't
    use any of these today, but plugin- or future-defined tools might, so
    this runs unconditionally rather than being opt-in.
    """
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key in ("additionalProperties", "$ref", "$defs", "definitions"):
                continue
            if key in ("oneOf", "anyOf", "allOf") and isinstance(value, list) and value:
                # Flatten to the first branch rather than dropping the
                # constraint entirely — an approximate schema that still
                # describes the common case beats no schema at all.
                out.update(_sanitize_schema_for_gemini(value[0]))
                continue
            out[key] = _sanitize_schema_for_gemini(value)
        return out
    if isinstance(schema, list):
        return [_sanitize_schema_for_gemini(v) for v in schema]
    return schema


def _to_gemini_tools(tool_defs: list[dict]):
    from google.genai import types

    declarations = []
    for td in tool_defs:
        fn = td.get("function", {})
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        declarations.append(types.FunctionDeclaration(
            name=fn.get("name", ""),
            description=fn.get("description", ""),
            parameters_json_schema=_sanitize_schema_for_gemini(params),
        ))
    return [types.Tool(function_declarations=declarations)] if declarations else []


def _to_gemini_contents(messages: list[dict]):
    """Convert OpenAI-format messages to Gemini `types.Content` turns.

    Returns (system_instruction, contents). A `tool`-role OpenAI message only
    carries `tool_call_id` + `content`, not the function name Gemini's
    function_response part requires — so we track name-by-call-id from each
    preceding assistant tool_calls turn as we walk the list.
    """
    from google.genai import types

    system_parts: list[str] = []
    contents = []
    name_by_call_id: dict[str, str] = {}

    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if role == "system":
            if content:
                system_parts.append(content)
            continue

        if role == "assistant" and m.get("tool_calls"):
            parts = []
            if content:
                parts.append(types.Part.from_text(text=content))
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                call_id = tc.get("id", "")
                if call_id:
                    name_by_call_id[call_id] = name
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                parts.append(types.Part(function_call=types.FunctionCall(name=name, args=args)))
            contents.append(types.Content(role="model", parts=parts))
            continue

        if role == "tool":
            call_id = m.get("tool_call_id", "")
            name = name_by_call_id.get(call_id, "")
            contents.append(types.Content(role="tool", parts=[
                types.Part.from_function_response(name=name, response={"result": str(content or "")}),
            ]))
            continue

        if role == "user" and content:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=content)]))
            continue

        if role == "assistant" and content:
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=content)]))
            continue

    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


class GeminiAdapter(LLMAdapter):
    last_usage: dict | None = None

    def __init__(self, base_url: str, api_key: str, model: str, extra: dict | None = None):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        extra = extra or {}
        self.project_id = extra.get("project_id") or None
        self.location = extra.get("location") or _DEFAULT_LOCATION

    def _build_client(self):
        from google import genai

        key = (self.api_key or "").strip()
        if key.startswith("{"):
            from google.oauth2 import service_account

            if not self.project_id:
                raise RuntimeError(
                    "Gemini provider has a service-account credential set but no Project ID — "
                    "set one in Settings → LLM Provider."
                )
            try:
                sa_info = json.loads(key)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Gemini service-account JSON is not valid JSON: {e}")
            credentials = service_account.Credentials.from_service_account_info(
                sa_info, scopes=[_CLOUD_PLATFORM_SCOPE],
            )
            return genai.Client(
                vertexai=True, project=self.project_id, location=self.location,
                credentials=credentials,
            )

        # Plain API key → Gemini Developer API (AI Studio), not Vertex.
        return genai.Client(api_key=self.api_key)

    def complete(
        self,
        messages: list[dict],
        tool_defs: list[dict],
    ) -> tuple[str, list[dict], int]:
        from google.genai import errors, types

        client = self._build_client()
        system_instruction, contents = _to_gemini_contents(messages)
        tools = _to_gemini_tools(tool_defs)

        config_kwargs: dict = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if tools:
            config_kwargs["tools"] = tools
            config_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO"),
            )

        try:
            resp = client.models.generate_content(
                model=self.model, contents=contents,
                config=types.GenerateContentConfig(**config_kwargs) if config_kwargs else None,
            )
        except errors.ServerError as e:
            raise TransientError(f"Gemini server error {e.code}: {e.message}")
        except errors.ClientError as e:
            if e.code == 429:
                raise TransientError(f"Gemini rate limit: {e.message}")
            raise RuntimeError(f"Gemini API error {e.code}: {e.message}")
        except errors.APIError as e:
            raise RuntimeError(f"Gemini API error {e.code}: {e.message}")

        try:
            response_text = resp.text or ""
        except Exception:
            # .text raises if the response has only function_call parts and
            # no text parts — not an error condition for a pure tool-call turn.
            response_text = ""

        tool_calls = []
        for fc in (resp.function_calls or []):
            tool_calls.append({
                "id": fc.id or str(uuid.uuid4()),
                "name": fc.name or "",
                "args": fc.args or {},
            })

        usage = resp.usage_metadata
        input_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = (usage.candidates_token_count or 0) if usage else 0
        self.last_usage = {"input": input_tokens, "output": output_tokens, "model": self.model}
        return response_text, tool_calls, input_tokens + output_tokens
