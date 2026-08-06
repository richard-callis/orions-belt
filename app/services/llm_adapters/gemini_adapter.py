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

# finish_reason values that mean the response was refused/blocked, not just
# "nothing more to say" (STOP) or "ran out of budget" (MAX_TOKENS, still a
# usable partial reply). Left as-is, a blocked call comes back as empty
# text + no tool_calls — indistinguishable from a legitimate empty reply.
_BLOCKED_FINISH_REASONS = {
    "SAFETY", "RECITATION", "LANGUAGE", "BLOCKLIST", "PROHIBITED_CONTENT",
    "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
}


def _to_gemini_tools(tool_defs: list[dict]):
    from google.genai import types

    declarations = []
    for td in tool_defs:
        fn = td.get("function", {})
        # No client-side stripping of additionalProperties/$ref/$defs/oneOf/
        # anyOf/allOf here — an earlier version did, on the assumption that
        # Gemini's function-calling schema is a strict OpenAPI 3.0 subset
        # that rejects them. Verified against the installed SDK that this
        # was wrong for this specific field: FunctionDeclaration.parameters_
        # json_schema's own docstring example includes
        # `"additionalProperties": false`, and stripping anyOf/$ref
        # empirically produces a WORSE schema than the original — e.g.
        # `anyOf: [{type: string}, {type: null}]` (an optional string)
        # silently became a bare `{}` (untyped, and no longer optional).
        # parameters_json_schema exists precisely to accept standard JSON
        # Schema; trust it rather than second-guessing it client-side.
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        declarations.append(types.FunctionDeclaration(
            name=fn.get("name", ""),
            description=fn.get("description", ""),
            parameters_json_schema=params,
        ))
    return [types.Tool(function_declarations=declarations)] if declarations else []


def _to_gemini_contents(messages: list[dict]):
    """Convert OpenAI-format messages to Gemini `types.Content` turns.

    Returns (system_instruction, contents). A `tool`-role OpenAI message only
    carries `tool_call_id` + `content`, not the function name Gemini's
    function_response part requires — so we track name-by-call-id from each
    preceding assistant tool_calls turn as we walk the list.

    Consecutive `tool`-role messages (the result of a single assistant turn
    that made N parallel tool_calls) are coalesced into ONE Content with N
    function_response parts, not N separate Content objects — Gemini
    requires the response turn for a parallel call to match the call turn
    part-for-part in a single Content. Verified empirically: constructing
    them as separate Contents (the naive one-message-in, one-Content-out
    approach) desyncs from the preceding call turn's part count, breaking
    exactly the surface this adapter targets (a chat room where an agent
    makes >1 tool call in a turn), even though a strictly-serial caller
    like the Task executor (one call per turn) never happens to hit it.
    """
    from google.genai import types

    system_parts: list[str] = []
    contents = []
    name_by_call_id: dict[str, str] = {}
    pending_tool_parts: list = []

    def _flush_pending_tool_parts():
        if pending_tool_parts:
            contents.append(types.Content(role="user", parts=list(pending_tool_parts)))
            pending_tool_parts.clear()

    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if role == "system":
            if content:
                system_parts.append(content)
            continue

        if role == "assistant" and m.get("tool_calls"):
            _flush_pending_tool_parts()
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
            # Gemini has no "tool" role — Content.role accepts only "user"
            # or "model" (confirmed against the field description AND the
            # SDK's own automatic-function-calling loop, which always
            # builds the function-response turn as role="user"). Getting
            # this wrong doesn't fail until the SECOND turn of a tool call
            # — the model successfully emits a function_call, but feeding
            # the result back 400s, so tools look like they're "not being
            # invoked" when the real failure is the round trip after.
            call_id = m.get("tool_call_id", "")
            name = name_by_call_id.get(call_id, "")
            pending_tool_parts.append(
                types.Part.from_function_response(name=name, response={"result": str(content or "")})
            )
            continue

        _flush_pending_tool_parts()

        if role == "user" and content:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=content)]))
            continue

        if role == "assistant" and content:
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=content)]))
            continue

    _flush_pending_tool_parts()

    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents



# genai.Client caches keyed by exactly the config that determines its
# identity — (api_key, project_id, location). A GeminiAdapter is
# constructed fresh on every single LLM call (dispatcher.get_adapter), so
# without this every call would mint a brand-new service-account
# Credentials object and do a fresh JWT->access-token exchange with
# Google's OAuth endpoint — an extra network round-trip per agent step,
# and it defeats the whole point of google.auth.credentials.Credentials
# objects, which are designed to be built once and reused: they refresh
# their own access token internally as it nears expiry. Keying by the
# actual config values (not e.g. a fixed singleton) means the cache
# self-invalidates the moment a user rotates the credential or changes
# project/location in Settings — no explicit invalidation needed.
_client_cache: dict[tuple, object] = {}


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
        from config import Config

        cache_key = (self.api_key, self.project_id, self.location)
        cached = _client_cache.get(cache_key)
        if cached is not None:
            return cached

        from google import genai

        timeout_ms = int(float(getattr(Config, "LLM_TIMEOUT", 600)) * 1000)
        http_options = genai.types.HttpOptions(timeout=timeout_ms)

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
            client = genai.Client(
                vertexai=True, project=self.project_id, location=self.location,
                credentials=credentials, http_options=http_options,
            )
        else:
            # Plain API key → Gemini Developer API (AI Studio), not Vertex.
            client = genai.Client(api_key=self.api_key, http_options=http_options)

        _client_cache[cache_key] = client
        return client

    def complete(
        self,
        messages: list[dict],
        tool_defs: list[dict],
    ) -> tuple[str, list[dict], int]:
        import httpx
        from google.auth import exceptions as google_auth_exceptions
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
        except (httpx.TransportError, google_auth_exceptions.GoogleAuthError) as e:
            # Network blips and OAuth token-refresh failures — neither is a
            # google.genai.errors.APIError subclass, so without this they'd
            # propagate uncaught and fail the run outright instead of
            # retrying. Vertex mode adds an OAuth round-trip the other
            # providers don't have, so this matters more here.
            raise TransientError(f"Gemini connection/auth error: {e}")

        if not resp.candidates:
            block_reason = getattr(resp.prompt_feedback, "block_reason", None) if resp.prompt_feedback else None
            raise RuntimeError(f"Gemini blocked the prompt (block_reason={block_reason})")

        finish_reason = getattr(resp.candidates[0].finish_reason, "name", resp.candidates[0].finish_reason)
        if finish_reason in _BLOCKED_FINISH_REASONS:
            # Without this, a blocked/policy-refused response comes back as
            # empty text + no tool_calls — indistinguishable from a
            # legitimate "nothing to do here" reply, so callers (e.g. the
            # Task executor) record it as a successfully completed run.
            raise RuntimeError(f"Gemini blocked the response (finish_reason={finish_reason})")

        # .text is Optional[str] — None (not a raise) for a response with
        # only function_call parts and no text parts, which is the normal
        # shape of a pure tool-call turn, not an error condition.
        response_text = resp.text or ""

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
        # total_token_count (not input+output) is the true billed total on
        # thinking models — it also includes thoughts_token_count, which
        # prompt/candidates alone leave out. This is what _check_token_budget
        # actually enforces against, so undercounting here means the budget
        # doesn't bind on exactly the models this adapter targets
        # (gemini-2.5-*), not just an underreported dashboard number.
        total_tokens = (usage.total_token_count or 0) if usage and usage.total_token_count else (input_tokens + output_tokens)
        return response_text, tool_calls, total_tokens
