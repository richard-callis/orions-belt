"""
OpenAI / OpenAI-compatible adapter.

Uses the openai SDK (already in requirements.txt) instead of raw httpx.
Handles any endpoint that speaks the OpenAI chat completions protocol:
OpenAI, Azure OpenAI, llama-server, LM Studio, Groq, etc.
"""
from __future__ import annotations
import json
import logging
import uuid

from app.services.llm_adapters.base import LLMAdapter
from app.services.llm import TransientError, RoleOrderError, ContextTooLargeError

log = logging.getLogger("orions-belt.adapters.openai")


class OpenAIAdapter(LLMAdapter):
    last_usage: dict | None = None

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def complete(
        self,
        messages: list[dict],
        tool_defs: list[dict],
    ) -> tuple[str, list[dict], int]:
        from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError
        from config import Config

        client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key or "none",
            timeout=float(getattr(Config, "LLM_TIMEOUT", 600)),
        )

        kwargs: dict = {"model": self.model, "messages": messages}
        if tool_defs:
            kwargs["tools"] = tool_defs

        try:
            resp = client.chat.completions.create(**kwargs)
        except APIStatusError as e:
            if e.status_code in (429, 500, 502, 503, 504, 529):
                raise TransientError(f"HTTP {e.status_code}: {e.message}")
            raise RuntimeError(f"OpenAI API error {e.status_code}: {e.message}")
        except (APIConnectionError, APITimeoutError) as e:
            raise TransientError(f"Connection error: {e}")

        choice = resp.choices[0]
        msg = choice.message
        response_text = msg.content or ""

        tool_calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({
                "id": tc.id or str(uuid.uuid4()),
                "name": tc.function.name,
                "args": args,
            })

        if resp.usage:
            input_tokens = resp.usage.prompt_tokens or 0
            output_tokens = resp.usage.completion_tokens or 0
        else:
            input_tokens = output_tokens = 0
        self.last_usage = {"input": input_tokens, "output": output_tokens, "model": self.model}
        return response_text, tool_calls, input_tokens + output_tokens
