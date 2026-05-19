"""Abstract base class for LLM adapters."""
from __future__ import annotations
from abc import ABC, abstractmethod


class LLMAdapter(ABC):
    """Each adapter wraps one provider's SDK or HTTP client.

    complete() must return (response_text, tool_calls, tokens) — identical
    signature to the old _call_llm_sync so retry_with_recovery needs no changes.
    """

    @abstractmethod
    def complete(
        self,
        messages: list[dict],
        tool_defs: list[dict],
    ) -> tuple[str, list[dict], int]:
        """Make a synchronous LLM call.

        Args:
            messages: OpenAI-format message list.
            tool_defs: OpenAI-format tool definition list (may be empty).

        Returns:
            (response_text, tool_calls, tokens_used)
            tool_calls: [{"id": str, "name": str, "args": dict}]
        """
        ...
