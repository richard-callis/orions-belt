"""
LLM Adapter package — per-provider adapters for LLM calls.

All adapters return (response_text: str, tool_calls: list[dict], tokens: int).
tool_calls items: {"id": str, "name": str, "args": dict}

Usage:
    from app.services.llm_adapters import get_adapter
    adapter = get_adapter(base_url, api_key, model)
    text, tool_calls, tokens = adapter.complete(messages, tool_defs)
"""
from app.services.llm_adapters.dispatcher import get_adapter

__all__ = ["get_adapter"]
