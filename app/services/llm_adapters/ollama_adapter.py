"""
Ollama adapter — uses the native Ollama Python client (/api/chat).

The native endpoint is more stable than the OpenAI-compat shim (/v1/chat/completions),
especially for tool calling and system prompts on non-llama models.
"""
from __future__ import annotations
import json
import logging
import uuid

from app.services.llm_adapters.base import LLMAdapter
from app.services.llm import TransientError

log = logging.getLogger("orions-belt.adapters.ollama")


def _to_ollama_tools(tool_defs: list[dict]) -> list[dict]:
    """Ollama tool format is the same as OpenAI — pass through unchanged."""
    return tool_defs


class OllamaAdapter(LLMAdapter):
    def __init__(self, base_url: str, api_key: str, model: str):
        # Strip /v1 suffix if present — ollama client uses the base host:port
        import re
        self.host = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
        self.model = model

    def complete(
        self,
        messages: list[dict],
        tool_defs: list[dict],
    ) -> tuple[str, list[dict], int]:
        try:
            import ollama
        except ImportError:
            raise RuntimeError("ollama package not installed — run: pip install ollama")

        client = ollama.Client(host=self.host, timeout=120.0)

        # Filter out unsupported roles for Ollama
        ollama_messages = [
            m for m in messages
            if m.get("role") in ("system", "user", "assistant", "tool")
        ]

        kwargs: dict = {"model": self.model, "messages": ollama_messages}
        if tool_defs:
            kwargs["tools"] = _to_ollama_tools(tool_defs)

        try:
            resp = client.chat(**kwargs)
        except ollama.ResponseError as e:
            if e.status_code in (429, 500, 503):
                raise TransientError(f"Ollama error {e.status_code}: {e.error}")
            raise RuntimeError(f"Ollama error: {e.error}")
        except Exception as e:
            if "connect" in str(e).lower() or "timeout" in str(e).lower():
                raise TransientError(f"Ollama connection error: {e}")
            raise RuntimeError(f"Ollama call failed: {e}")

        msg = resp.message
        response_text = msg.content or ""

        tool_calls = []
        for tc in (msg.tool_calls or []):
            fn = tc.function
            tool_calls.append({
                "id": str(uuid.uuid4()),  # Ollama doesn't return tool IDs
                "name": fn.name,
                "args": fn.arguments or {},
            })

        # Ollama doesn't return token counts in all versions
        tokens = getattr(resp, "eval_count", 0) or 0
        return response_text, tool_calls, tokens
