"""
Anthropic adapter — uses the anthropic SDK directly.

Benefits over the OpenAI-compat shim:
- Native streaming event types
- Extended thinking support
- Prompt caching headers
- Accurate error types (overloaded_error vs rate_limit_error)
"""
from __future__ import annotations
import json
import logging
import uuid

from app.services.llm_adapters.base import LLMAdapter
from app.services.llm import TransientError, ContextTooLargeError

log = logging.getLogger("orions-belt.adapters.anthropic")

# Anthropic uses a different max_tokens default — must be set explicitly
_DEFAULT_MAX_TOKENS = 8096


def _to_anthropic_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Convert OpenAI-format messages to Anthropic format.

    Returns (system_prompt, anthropic_messages).
    Anthropic expects system as a top-level param, not in the messages array.
    Tool result messages use role "user" with a tool_result content block.
    """
    system_parts: list[str] = []
    out = []

    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if role == "system":
            # Multiple system messages (agent prompt + injected knowledge +
            # compaction notices) must all be preserved, not overwritten.
            if content:
                system_parts.append(content)
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic user message with tool_result block
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": str(content or ""),
                }],
            })
            continue

        if role == "assistant" and m.get("tool_calls"):
            # OpenAI assistant tool_calls → Anthropic tool_use content blocks
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                try:
                    inp = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    inp = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", str(uuid.uuid4())),
                    "name": fn.get("name", ""),
                    "input": inp,
                })
            out.append({"role": "assistant", "content": blocks})
            continue

        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return system_prompt, out


def _to_anthropic_tools(tool_defs: list[dict]) -> list[dict]:
    """Convert OpenAI tool definitions to Anthropic format."""
    result = []
    for td in tool_defs:
        fn = td.get("function", {})
        result.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


class AnthropicAdapter(LLMAdapter):
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def complete(
        self,
        messages: list[dict],
        tool_defs: list[dict],
    ) -> tuple[str, list[dict], int]:
        import anthropic

        client_kwargs: dict = {"api_key": self.api_key, "timeout": 120.0}
        if self.base_url and "api.anthropic.com" not in self.base_url.lower():
            client_kwargs["base_url"] = self.base_url
        client = anthropic.Anthropic(**client_kwargs)
        system_prompt, anthropic_messages = _to_anthropic_messages(messages)
        anthropic_tools = _to_anthropic_tools(tool_defs) if tool_defs else []

        kwargs: dict = {
            "model": self.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": anthropic_messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        try:
            resp = client.messages.create(**kwargs)
        except anthropic.APIConnectionError as e:
            raise TransientError(f"Anthropic connection error: {e}")
        except anthropic.RateLimitError as e:
            raise TransientError(f"Anthropic rate limit: {e}")
        except anthropic.APIStatusError as e:
            # RateLimitError is a subclass of APIStatusError (caught above);
            # this handles 5xx (incl. 529 overloaded) and 400 uniformly without
            # depending on SDK-version-specific exception classes.
            status = getattr(e, "status_code", None)
            if status in (500, 502, 503, 529):
                raise TransientError(f"Anthropic server error {status}: {e}")
            if status == 400 and ("context" in str(e).lower() or "too long" in str(e).lower()):
                raise ContextTooLargeError(f"Context too large: {e}")
            raise RuntimeError(f"Anthropic API error {status}: {e}")

        # Extract text and tool use blocks
        response_text = ""
        tool_calls = []

        text_parts = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "args": block.input or {},
                })

        response_text = "".join(text_parts)
        tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
        return response_text, tool_calls, tokens
