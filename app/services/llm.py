"""
Orion's Belt — LLM Service
Shared utilities for chat: context window management, tool definitions, message persistence.

The streaming generators (_stream_openai_gen, _stream_ollama_gen) are in chat.py
since they need direct access to Flask's stream_with_context.
"""
import json
import uuid
from datetime import datetime, timezone

from app import db
from app.models.chat import Message, ContextCompaction
from app.models.mcp_tool import MCPTool


def _now():
    return datetime.now(timezone.utc)


# ── Context window helpers ────────────────────────────────────────────────────

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
    # Roles that are LLM-safe to include in context
    LLM_ROLES = {"user", "assistant", "system"}

    all_msgs = [m for m in messages if getattr(m, 'role', m.get('role', '')) in LLM_ROLES]
    total = len(all_msgs)

    if strategy == "sliding":
        return [
            {"role": m.role, "content": m.content}
            for m in all_msgs[-history_limit:]
        ]
    elif strategy == "summarize" and total > summarize_after:
        recent = all_msgs[-history_limit:]
        return (
            [{"role": "system", "content": "[Previous conversation summarized]"}]
            + [{"role": m.role, "content": m.content} for m in recent]
        )
    else:
        return [{"role": m.role, "content": m.content} for m in all_msgs[-history_limit:]]


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

def build_tool_definitions(tools) -> list[dict]:
    """Convert MCPTool models to OpenAI/Ollama tool definition format."""
    result = []
    for tool in tools:
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
    return result
