"""
Provider dispatcher — detects the right adapter from provider config.

The base_url determines the wire protocol, so an EXPLICIT url always wins over
the model-name heuristic. Otherwise a `claude-*`/`fable-*` model override would
mis-route to the native Anthropic protocol even when the active provider is
OpenAI/Ollama/OpenRouter (which proxy those models over the OpenAI protocol).

Detection order:
1. url contains :11434 or ollama       → OllamaAdapter
2. url contains anthropic              → AnthropicAdapter
3. url set (any other endpoint)        → OpenAIAdapter (generic compat)
4. url empty + model claude/fable      → AnthropicAdapter (native default)
5. url empty otherwise                 → OpenAIAdapter
"""
from __future__ import annotations
import logging

from app.services.llm_adapters.base import LLMAdapter

log = logging.getLogger("orions-belt.adapters.dispatcher")


def get_adapter(base_url: str, api_key: str, model: str) -> LLMAdapter:
    """Return the appropriate adapter for the given provider config."""
    url = (base_url or "").strip().lower()
    model_lower = (model or "").lower()

    # 1-2: explicit endpoints (the URL tells us the protocol).
    if ":11434" in url or "ollama" in url:
        log.debug("adapter=ollama model=%s", model)
        from app.services.llm_adapters.ollama_adapter import OllamaAdapter
        return OllamaAdapter(base_url, api_key, model)

    if "anthropic" in url:
        log.debug("adapter=anthropic (url) model=%s", model)
        from app.services.llm_adapters.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(base_url, api_key, model)

    # 3: any other explicit endpoint speaks the OpenAI protocol, even if the
    # model is named claude/fable (a compat gateway proxies it).
    if url:
        log.debug("adapter=openai (explicit url) model=%s", model)
        from app.services.llm_adapters.openai_adapter import OpenAIAdapter
        return OpenAIAdapter(base_url, api_key, model)

    # 4-5: no URL configured — fall back to the model-name heuristic.
    if model_lower.startswith("claude") or model_lower.startswith("fable"):
        log.debug("adapter=anthropic (model heuristic) model=%s", model)
        from app.services.llm_adapters.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(base_url, api_key, model)

    log.debug("adapter=openai (default) model=%s", model)
    from app.services.llm_adapters.openai_adapter import OpenAIAdapter
    return OpenAIAdapter(base_url, api_key, model)
