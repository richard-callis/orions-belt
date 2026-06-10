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


# ── Context window helpers ────────────────────────────────────────────────────

# Approximate tokens per character (used for threshold estimation)
CHARS_PER_TOKEN = 4

# Context compaction thresholds (from harness spec)
# Based on percentage of context window used
# The "window" is estimated as history_limit * average_msg_chars / CHARS_PER_TOKEN
CONTEXT_THRESHOLD_WARN = 0.70    # 70% — log warning, prepare summary
CONTEXT_THRESHOLD_COMPACT = 0.90  # 90% — auto-compact oldest messages
CONTEXT_THRESHOLD_EMERGENCY = 0.99  # 99% — emergency reset


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
        content = m.get("content", "")
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
    # Add token usage percentage
    total_tokens = sum(_estimate_tokens(m) for m in context)
    estimated_window = history_limit * 150  # ~150 tokens per message average
    state["token_usage_pct"] = min((total_tokens / max(estimated_window, 1)) * 100, 999)
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

    # Estimate token usage for threshold check
    estimated_window = history_limit * 150  # ~150 tokens per message average
    total_tokens = sum(_estimate_tokens({"content": m.content}) for m in all_msgs)
    usage_pct = (total_tokens / max(estimated_window, 1)) * 100

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


def _call_llm_sync(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    tool_defs: list,
) -> tuple[str, list, int]:
    """Make a synchronous LLM call via the appropriate provider adapter.

    Returns (response_text, tool_calls, tokens_used).
    Raises TransientError, RoleOrderError, ContextTooLargeError, or RuntimeError.
    """
    from app.services.llm_adapters import get_adapter
    adapter = get_adapter(base_url, api_key, model)
    return adapter.complete(messages, tool_defs)


def retry_with_recovery(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    tool_defs: list,
    max_retries: int = 3,
) -> tuple[str, list, int]:
    """Retry an LLM call with recovery strategies.

    When an LLM call fails, try recovery strategies in order:
    1. Transient errors (429, 500, 503) → retry with exponential backoff
    2. Role ordering error → drop tools from prompt → retry
    3. Context too large → compact messages → retry
    4. All else fails → raise error

    Returns: (response_text, tool_calls, tokens)
    Raises: RuntimeError on unrecoverable failure
    """
    attempts = 0
    tools_dropped = False

    while attempts < max_retries:
        attempts += 1
        try:
            return _call_llm_sync(base_url, api_key, model, messages, tool_defs)
        except RecoveryError as e:
            log.warning("LLM call failed (attempt %d/%d): %s — strategy: %s",
                        attempts, max_retries, e, e.strategy)

            if e.strategy == "retry_backoff":
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
