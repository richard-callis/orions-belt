"""
Orion's Belt — LLM Service
Shared utilities for chat: context window management, tool definitions, message persistence.

The streaming generators (_stream_openai_gen, _stream_ollama_gen) are in chat.py
since they need direct access to Flask's stream_with_context.
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx

from app import db
from app.models.chat import Message, ContextCompaction
from app.models.mcp_tool import MCPTool

log = logging.getLogger("orions-belt.agents")


def _now():
    return datetime.now(timezone.utc)


def provider_extra(provider: dict) -> dict:
    """Narrow a decrypted provider dict down to just the non-secret fields
    get_adapter's `extra` param actually needs (currently Gemini's
    project_id/location). Callers that have the full provider dict handy
    (it already carries a decrypted credential — for Vertex, the whole
    service-account JSON including the private key) should pass this
    narrowed copy to retry_with_recovery rather than the dict itself, so a
    future adapter or a debug/logging change that dumps `extra` doesn't
    widen the blast radius of that credential for no reason — nothing
    downstream of `extra` needs more than these two fields today."""
    return {"project_id": provider.get("project_id"), "location": provider.get("location")}


# ── Context window helpers ────────────────────────────────────────────────────

# Approximate tokens per character (used for threshold estimation)
CHARS_PER_TOKEN = 4

# Context compaction thresholds — percentage of the model's context window used.
CONTEXT_THRESHOLD_WARN = 70      # 70% — log warning, prepare summary
CONTEXT_THRESHOLD_COMPACT = 90   # 90% — auto-compact oldest messages
CONTEXT_THRESHOLD_EMERGENCY = 99  # 99% — emergency reset

# Default assumed model context window in tokens. Used as the denominator for
# the usage-percentage thresholds above. Configurable via Config.CONTEXT_WINDOW_TOKENS
# (set it lower for small local models). NOT history_limit*150 — that tiny fake
# window made every normal conversation read as ">99% — emergency".
_DEFAULT_CONTEXT_WINDOW = 128000


def _context_window_tokens() -> int:
    try:
        from config import Config
        return int(getattr(Config, "CONTEXT_WINDOW_TOKENS", _DEFAULT_CONTEXT_WINDOW))
    except Exception:
        return _DEFAULT_CONTEXT_WINDOW


def _estimate_tokens(msg: dict) -> int:
    """Estimate token count for a message dict."""
    content = msg.get("content", "")
    if isinstance(msg.get("content"), str):
        return len(content) // CHARS_PER_TOKEN
    # Handle list-style content (some providers use array of content blocks)
    if isinstance(content, list):
        text = " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return len(text) // CHARS_PER_TOKEN
    return len(str(content)) // CHARS_PER_TOKEN


def truncate_history(messages: list[dict], max_msg_chars: int = 4000) -> list[dict]:
    """Truncate long messages to prevent context bloat.

    Mirrors Orion's approach: cap individual message size to prevent
    bloated tool output / API dumps from consuming the entire context.
    """
    result = []
    for m in messages:
        content = m.get("content")
        # Assistant tool_calls messages carry content=None; non-str content
        # (block lists) is passed through untouched.
        if not isinstance(content, str):
            result.append(m)
            continue
        if len(content) > max_msg_chars:
            result.append({**m, "content": content[:max_msg_chars] + "\n[…truncated]"})
        else:
            result.append(m)
    return result


def build_context(
    messages,
    strategy: str = "sliding",
    history_limit: int = 30,
    summarize_after: int = 50,
) -> list[dict]:
    """Build message history for the LLM context window.

    Supports three strategies:
    - full: all messages (small conversations)
    - sliding: last N messages
    - summarize: summary of old + last N recent

    Note: tool_call messages (our display-only role) are filtered out — they
    can't be sent to the LLM because OpenAI rejects unknown roles. The tool
    result messages (role="tool") are also excluded since without their
    paired assistant tool_calls message they'd cause API errors.
    """
    return _build_context_with_state(messages, strategy, history_limit, summarize_after)[0]


def build_context_with_state(
    messages,
    strategy: str = "sliding",
    history_limit: int = 30,
    summarize_after: int = 50,
) -> tuple[list[dict], dict]:
    """Build message history with compaction state info.

    Returns:
        (context_messages, state_dict)
        state_dict contains:
            - 'needs_compaction': bool
            - 'threshold_level': 'normal' | 'warning' | 'compact' | 'emergency'
            - 'token_usage_pct': float (0-100)
            - 'summary_text': str (the compaction summary, if applicable)
            - 'messages_compacted': int (number of messages compacted)
            - 'archived_ids': list[str] (message IDs that were compacted)
    """
    context, state = _build_context_with_state(
        messages, strategy, history_limit, summarize_after
    )
    # Add token usage percentage against the real model window. Report the pct
    # for the FULL history (pre-compaction), so it's consistent with the
    # threshold_level the state machine computed.
    total_tokens = sum(_estimate_tokens({"content": m.content}) for m in messages if m.role in ("user", "assistant", "system"))
    window = _context_window_tokens()
    state["token_usage_pct"] = min((total_tokens / max(window, 1)) * 100, 999)
    state["needs_compaction"] = state["threshold_level"] in ("compact", "emergency")
    return context, state


def _build_context_with_state(
    messages,
    strategy: str = "sliding",
    history_limit: int = 30,
    summarize_after: int = 50,
) -> tuple[list[dict], dict]:
    """Core context builder with threshold state machine (from harness spec).

    Returns (context_messages, state_dict).
    """
    LLM_ROLES = {"user", "assistant", "system"}
    all_msgs = [m for m in messages if m.role in LLM_ROLES]
    total = len(all_msgs)

    state = {
        "threshold_level": "normal",
        "summary_text": None,
        "messages_compacted": 0,
        "archived_ids": [],
    }

    # Estimate token usage for threshold check against the real model window.
    window = _context_window_tokens()
    total_tokens = sum(_estimate_tokens({"content": m.content}) for m in all_msgs)
    usage_pct = (total_tokens / max(window, 1)) * 100

    if usage_pct > CONTEXT_THRESHOLD_EMERGENCY:
        # >99% — emergency reset: keep only last N messages + system prompt
        state["threshold_level"] = "emergency"
        recent = all_msgs[-history_limit:]
        return (
            [{"role": "system", "content": "[Conversation context reset — only recent messages retained]"}]
            + [{"role": m.role, "content": m.content} for m in recent],
            state,
        )

    if usage_pct > CONTEXT_THRESHOLD_COMPACT:
        # 90-99% — auto-compact oldest messages
        state["threshold_level"] = "compact"
        old_msgs = all_msgs[:-history_limit]
        state["messages_compacted"] = len(old_msgs)
        state["archived_ids"] = [m.id for m in old_msgs]
        state["summary_text"] = "[Previous messages compacted to free context space]"
        recent = all_msgs[-history_limit:]
        context_parts = []
        if old_msgs:
            context_parts.append({
                "role": "system",
                "content": state["summary_text"],
            })
        context_parts.extend({"role": m.role, "content": m.content} for m in recent)
        return context_parts, state

    if usage_pct > CONTEXT_THRESHOLD_WARN and strategy == "summarize":
        # 70-90% — warning level with summarize strategy
        state["threshold_level"] = "warning"
        state["summary_text"] = "[Previous conversation summarized]"
        recent = all_msgs[-history_limit:]
        return (
            [{"role": "system", "content": "[Previous conversation summarized]"}]
            + [{"role": m.role, "content": m.content} for m in recent],
            state,
        )

    # Normal path
    if strategy == "sliding":
        return (
            [{"role": m.role, "content": m.content} for m in all_msgs[-history_limit:]],
            state,
        )
    elif strategy == "summarize" and total > summarize_after:
        state["threshold_level"] = "warning"
        state["summary_text"] = "[Previous conversation summarized]"
        recent = all_msgs[-history_limit:]
        return (
            [{"role": "system", "content": "[Previous conversation summarized]"}]
            + [{"role": m.role, "content": m.content} for m in recent],
            state,
        )
    else:
        return (
            [{"role": m.role, "content": m.content} for m in all_msgs[-history_limit:]],
            state,
        )


# ── Message persistence ───────────────────────────────────────────────────────

def save_assistant_message(session_id: str, content: str) -> None:
    """Save the assistant's response after stream completes."""
    if not content or not content.strip():
        return
    msg = Message(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=content[:4000],
        created_at=_now(),
        token_count=len(content) // 4,
    )
    db.session.add(msg)


# ── Tool definition builder ───────────────────────────────────────────────────

def build_tool_definitions(tools, include_plugins: bool = True) -> list[dict]:
    """Convert MCPTool models to OpenAI/Ollama tool definition format.

    Automatically filters out unavailable tools (e.g., search_emails on
    non-Windows, run_sql_query without pyodbc) using the availability
    checker from the harness spec.

    Merges in plugin-registered tools (from extensions/ directory).
    """
    from app.services.mcp.availability import is_tool_available

    result = []
    for tool in tools:
        if not is_tool_available(tool.name, tool.enabled):
            continue
        schema = json.loads(tool.input_schema or "{}")
        result.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": schema if schema else {
                    "type": "object",
                    "properties": {},
                },
            },
        })

    # Merge plugin-registered tools
    if include_plugins:
        try:
            from app.services.plugins import get_plugin_manager
            plugin_defs = get_plugin_manager().get_tool_definitions()
            # Avoid duplicates — skip if a built-in tool already has the same name
            builtin_names = {d["function"]["name"] for d in result}
            for pd in plugin_defs:
                if pd["function"]["name"] not in builtin_names:
                    result.append(pd)
        except Exception:
            pass  # Plugin system failure is non-fatal

    return result


# ── Knowledge context injection ───────────────────────────────────────────────

def inject_knowledge_context(messages: list[dict], query: str) -> list[dict]:
    """Prepend relevant llm-context notes to the message list.

    Selects up to 5 notes by keyword overlap against the query, then
    injects them as a system message after any existing system messages.
    Non-fatal: returns messages unchanged on any error.
    """
    try:
        from app.models.knowledge import Note
        context_notes = (
            Note.query.filter_by(note_type="llm-context")
            .order_by(Note.pinned.desc(), Note.updated_at.desc())
            .limit(20).all()
        )
        if not context_notes:
            return messages
        query_words = set(query.lower().split())

        def _relevance(note):
            text = f"{note.title} {note.content}".lower()
            return sum(1 for w in query_words if w in text)

        ranked = sorted(context_notes, key=_relevance, reverse=True)[:5]
        if not ranked:
            return messages

        ctx = "Relevant context from the knowledge base:\n\n"
        for note in ranked:
            ctx += f"### {note.title}\n{note.content}\n\n"

        sys_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]
        return sys_msgs + [{"role": "system", "content": ctx.strip()}] + other_msgs
    except Exception:
        return messages


# ── Error recovery types (from harness FALLBACK spec) ─────────────────────────

class RecoveryError(Exception):
    """Base class for recoverable LLM errors."""
    def __init__(self, message, strategy: str = "retry"):
        super().__init__(message)
        self.strategy = strategy


class TransientError(RecoveryError):
    """Temporary error — retry with backoff."""
    def __init__(self, message):
        super().__init__(message, strategy="retry_backoff")


class RoleOrderError(RecoveryError):
    """LLM rejected role ordering (role: "tool") — drop tools and retry."""
    def __init__(self, message):
        super().__init__(message, strategy="drop_tools")


class ContextTooLargeError(RecoveryError):
    """Context exceeds model window — compact and retry."""
    def __init__(self, message):
        super().__init__(message, strategy="compact_and_retry")


def _estimate_llm_cost_and_savings(
    model: str, input_tokens: int, output_tokens: int
) -> tuple[float | None, float | None]:
    """Estimated USD (cost, savings) for one LLM call — exactly one of the
    pair is non-None, mirroring orion-web's split: a self-hosted model's
    $ value is money *avoided*, not money spent, so it's tracked separately
    rather than folded into "cost" (which would misrepresent actual spend)
    or discarded (which would hide the value self-hosting provides).

    Returns (None, None) if the model has no configured pricing at all —
    genuinely unknown, not zero.
    """
    import json
    from app.models.settings import Setting
    try:
        raw = Setting.get("llm.model_pricing")
        pricing = json.loads(raw) if raw else {}
    except Exception:
        return None, None
    entry = pricing.get(model) if isinstance(pricing, dict) else None
    if not entry:
        return None, None
    input_price = entry.get("input_per_1m")
    output_price = entry.get("output_per_1m")
    if input_price is None and output_price is None:
        return None, None
    usd = (input_tokens * (input_price or 0) + output_tokens * (output_price or 0)) / 1_000_000
    if entry.get("self_hosted"):
        return None, usd
    return usd, None


def _to_jsonable(obj):
    """Best-effort conversion of an SDK request/response object to a plain
    JSON-serializable structure, for LLM traffic capture. SDK response
    objects are typically pydantic models (openai/anthropic/ollama all use
    pydantic) but this doesn't assume that specifically — falls back
    gracefully so a capture failure never breaks the actual LLM call it's
    observing (see _log_llm_call, which also wraps all of this in a
    broad try/except for the same reason)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        # A bare str(obj) here would look like a legitimate captured value
        # (e.g. a plain string field) with no indication it's actually a
        # conversion failure — wrap it so a human reading the capture can
        # tell the difference between "the API returned this string" and
        # "we couldn't serialize this object".
        return {"_capture_unconverted": repr(obj)[:2000]}


# Cap on persisted request/response JSON — generous enough for real
# debugging (a full tool-heavy conversation turn) while bounding storage on
# a table with no retention policy of its own.
_MAX_TRAFFIC_CAPTURE_CHARS = 200_000


def _capture_traffic_json(adapter) -> tuple[str | None, str | None]:
    """Redacted, size-capped JSON strings for adapter.last_request/
    last_response, or (None, None) if either is absent. Only called when
    debug.llm is enabled — see _log_llm_call.

    Both redaction layers apply, same as AuditLog: redact_deep walks the
    structure BEFORE serialization so a field literally named "api_key" or
    "authorization" is masked outright no matter how deeply it's nested in
    the provider payload (redact_text alone, pattern-matching the final
    string, would only catch values that happen to look like a known secret
    shape — a plain opaque token under a sensitive field name wouldn't
    match any pattern). redact_text still runs afterward on the full text
    to catch secrets embedded in ordinary string values (e.g. an echoed
    header) that field-name masking wouldn't touch.
    """
    from app.services.redact import redact_deep, redact_text

    def _dump(attr):
        raw = getattr(adapter, attr, None)
        if raw is None:
            return None
        try:
            text = json.dumps(redact_deep(_to_jsonable(raw)), indent=2, default=str)
        except Exception:
            return None
        text = redact_text(text)
        if len(text) > _MAX_TRAFFIC_CAPTURE_CHARS:
            omitted = len(text) - _MAX_TRAFFIC_CAPTURE_CHARS
            text = (text[:_MAX_TRAFFIC_CAPTURE_CHARS] +
                    f"\n... [TRUNCATED: {omitted} more characters omitted]")
        return text

    return _dump("last_request"), _dump("last_response")


def _log_llm_call(adapter, model: str, session_id: str | None, run_id: str | None,
                   latency_ms: int, success: bool, error: str | None = None) -> None:
    """Best-effort LLMLog write — logging must never break the actual LLM call
    it's observing, so any failure here is swallowed (debug-logged only)."""
    try:
        from app import db
        from app.models.logs import LLMLog
        from app.models.settings import Setting
        usage = getattr(adapter, "last_usage", None) or {}
        input_tokens = usage.get("input", 0)
        output_tokens = usage.get("output", 0)
        cost, savings = _estimate_llm_cost_and_savings(model, input_tokens, output_tokens) if success else (None, None)

        request_json = response_json = None
        # `is True`, not truthy — Setting.get returns the RAW STRING when a
        # key was last written with value_type="string" (POST /api/settings
        # writes every key that way, unconditionally; only PUT honors
        # _BOOL_KEYS). "false" is a non-empty string, and bool("false") is
        # True — so a naive truthy check turns capture ON while the
        # Settings UI toggle (which reads `d.data?.value === true`) still
        # renders OFF. That divergence is the worst failure mode a privacy
        # gate can have: verified empirically that Setting.set("debug.llm",
        # "false", value_type="string") left this branch active.
        if Setting.get("debug.llm", False) is True:
            request_json, response_json = _capture_traffic_json(adapter)

        db.session.add(LLMLog(
            provider=type(adapter).__name__.replace("Adapter", "").lower(),
            model=model, session_id=session_id, run_id=run_id,
            tokens_in=input_tokens, tokens_out=output_tokens,
            latency_ms=latency_ms, estimated_cost_usd=cost, estimated_savings_usd=savings,
            success=success, error=(error[:2000] if error else None),
            request_json=request_json, response_json=response_json,
        ))
        db.session.commit()
    except Exception as e:
        log.debug("LLMLog write failed (non-fatal): %s", e)
        try:
            from app import db
            db.session.rollback()
        except Exception:
            pass


def _call_llm_sync(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    tool_defs: list,
    session_id: str | None = None,
    run_id: str | None = None,
    extra: dict | None = None,
) -> tuple[str, list, int]:
    """Make a synchronous LLM call via the appropriate provider adapter.

    Returns (response_text, tool_calls, tokens_used).
    Raises TransientError, RoleOrderError, ContextTooLargeError, or RuntimeError.

    Every call (success or failure, including individual retry attempts) is
    logged to LLMLog via the adapter's `last_usage` — this is the single choke
    point every LLM call in the app passes through, so it's the one place
    that can capture usage/cost without threading session_id/run_id through
    every caller's business logic.

    `extra` carries provider-specific fields that don't fit base_url/api_key/
    model — currently just Gemini's project_id/location for Vertex mode.
    """
    from app.services.llm_adapters import get_adapter
    adapter = get_adapter(base_url, api_key, model, extra=extra)
    start = time.time()
    try:
        result = adapter.complete(messages, tool_defs)
    except Exception as e:
        _log_llm_call(adapter, model, session_id, run_id,
                      int((time.time() - start) * 1000), success=False, error=str(e))
        raise
    _log_llm_call(adapter, model, session_id, run_id,
                  int((time.time() - start) * 1000), success=True)
    return result


def retry_with_recovery(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    tool_defs: list,
    max_retries: int = 3,
    session_id: str | None = None,
    run_id: str | None = None,
    extra: dict | None = None,
) -> tuple[str, list, int]:
    """Retry an LLM call with recovery strategies.

    When an LLM call fails, try recovery strategies in order:
    1. Transient errors (429, 500, 503) → retry with exponential backoff
    2. Role ordering error → drop tools from prompt → retry
    3. Context too large → compact messages → retry
    4. All else fails → raise error

    `session_id`/`run_id` are optional — only used to attribute LLMLog rows,
    never required for the call itself. `extra` is provider-specific config
    (currently just Gemini's project_id/location) passed straight through to
    get_adapter().

    Returns: (response_text, tool_calls, tokens)
    Raises: RuntimeError on unrecoverable failure
    """
    attempts = 0
    tools_dropped = False

    while attempts < max_retries:
        attempts += 1
        try:
            return _call_llm_sync(base_url, api_key, model, messages, tool_defs,
                                  session_id=session_id, run_id=run_id, extra=extra)
        except RecoveryError as e:
            log.warning("LLM call failed (attempt %d/%d): %s — strategy: %s",
                        attempts, max_retries, e, e.strategy)

            if e.strategy == "retry_backoff":
                # On the last attempt, surface the REAL error instead of falling
                # through to the generic "retry loop exited" message.
                if attempts >= max_retries:
                    raise RuntimeError(f"LLM call failed after {max_retries} attempts: {e}")
                backoff = min(2 ** attempts, 8)
                log.info("Backing off %ds before retry", backoff)
                time.sleep(backoff)
                continue

            elif e.strategy == "drop_tools":
                if not tools_dropped:
                    log.info("Dropping tools from prompt and retrying")
                    tool_defs = []
                    tools_dropped = True
                    continue
                raise RuntimeError(f"LLM rejected tool calls even without tools: {e}")

            elif e.strategy == "compact_and_retry":
                non_system = [m for m in messages if m.get("role") != "system"]
                if len(non_system) > 4:
                    compact_count = len(non_system) // 2
                    messages = (
                        [m for m in messages if m.get("role") == "system"]
                        + [{"role": "system", "content": "[Previous conversation summarized — context truncated]"}]
                        + non_system[-compact_count:]
                    )
                    log.info("Compacted %d messages, retrying", len(non_system) - compact_count)
                    continue
                raise RuntimeError(f"Context too large and cannot compact further: {e}")

        except Exception as e:
            log.warning("LLM call failed (attempt %d/%d): %s", attempts, max_retries, e)
            if attempts >= max_retries:
                raise RuntimeError(f"LLM call failed after {max_retries} attempts: {e}")
            time.sleep(min(2 ** attempts, 4))

    raise RuntimeError("Unexpected: retry loop exited without raising")
