"""
Provider dispatcher — detects the right adapter from provider config.

Detection order:
1. base_url contains api.anthropic.com  → AnthropicAdapter
2. model name starts with "claude"      → AnthropicAdapter
3. base_url contains :11434 or ollama   → OllamaAdapter
4. Everything else                      → OpenAIAdapter (generic compat)
"""
from __future__ import annotations
import logging

from app.services.llm_adapters.base import LLMAdapter

log = logging.getLogger("orions-belt.adapters.dispatcher")


def get_adapter(base_url: str, api_key: str, model: str) -> LLMAdapter:
    """Return the appropriate adapter for the given provider config."""
    url = (base_url or "").lower()
    model_lower = (model or "").lower()

    if "api.anthropic.com" in url or "anthropic" in url or model_lower.startswith("claude") or model_lower.startswith("fable"):
        log.debug("adapter=anthropic model=%s", model)
        from app.services.llm_adapters.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(base_url, api_key, model)

    if ":11434" in url or "ollama" in url:
        log.debug("adapter=ollama model=%s", model)
        from app.services.llm_adapters.ollama_adapter import OllamaAdapter
        return OllamaAdapter(base_url, api_key, model)

    log.debug("adapter=openai model=%s", model)
    from app.services.llm_adapters.openai_adapter import OpenAIAdapter
    return OpenAIAdapter(base_url, api_key, model)
