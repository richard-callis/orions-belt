"""
AgentRuntime — an Agent's execution capability.

Domain model: **Chats · Agents · Tools**. Tools belong to Agents, not to chats.
An Agent runs its tools the same way in any context — a chat room, a task, or a
future cron/trigger — so every surface constructs an AgentRuntime and calls it,
and tool access + tiering are identical everywhere.

    rt = AgentRuntime(agent)              # resolves the active provider + model
    reply = rt.chat_reply(messages)       # one reply, running a bounded tool loop
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("orions-belt.agent-runtime")

# Tools at/above this tier are destructive and are NOT auto-run in a
# conversational context — they must go through a Task where they can be
# explicitly approved.
TIER_HARD_STOP = 3

_DEFAULT_MAX_TOOL_ITERS = 5


def resolve_active_provider() -> dict:
    """The active LLM provider config (base_url, api_key decrypted, model)."""
    from app.routes.settings import _get_active_provider
    return _get_active_provider() or {}


class AgentRuntime:
    """Wraps an Agent so it can think and act (run tools) in any context."""

    def __init__(self, agent, provider: dict | None = None):
        self.agent = agent
        self.provider = provider if provider is not None else resolve_active_provider()
        self.base_url = self.provider.get("base_url")
        self.api_key = self.provider.get("api_key")
        self.model = agent.llm_model_override or self.provider.get("model")

    # ── Tools (owned by the agent) ────────────────────────────────────────────

    def tools(self) -> list:
        """The MCP tools this agent may use: its allowed_tools, or all enabled
        tools when it has no explicit allowlist."""
        from app.models.mcp_tool import MCPTool
        try:
            allowed = json.loads(self.agent.allowed_tools or "[]")
        except Exception:
            allowed = []
        q = MCPTool.query.filter_by(enabled=True)
        if allowed:
            q = q.filter(MCPTool.name.in_(allowed))
        return q.all()

    def _authorized_dirs_block(self) -> str:
        """A system-prompt block listing authorized directories, so an agent
        with file tools knows what absolute paths are actually valid instead
        of guessing (e.g. a bare relative path like "notes") and getting
        refused with no way to self-correct. Empty string if the agent has no
        file-touching tools or none are configured."""
        file_tool_names = {"read_file", "list_directory", "search_files", "create_file",
                            "append_to_file", "modify_file", "create_directory",
                            "delete_file", "move_file", "create_word_document",
                            "create_powerpoint", "create_excel", "create_pdf"}
        if not any(t.name in file_tool_names for t in self.tools()):
            return ""
        from app.models.connector import AuthorizedDirectory
        dirs = AuthorizedDirectory.query.filter_by(enabled=True).all()
        if not dirs:
            return ""
        listing = "\n".join(f"- {d.alias}: {d.path}" for d in dirs)
        return (
            "\n\n## Authorized directories\n"
            "File tools only work with ABSOLUTE paths under one of these directories:\n"
            f"{listing}"
        )

    def run_tool(self, name: str, args: dict, tier: int = 0,
                 allow_tier: int = TIER_HARD_STOP - 1,
                 session_id: str | None = None, run_id: str | None = None) -> str:
        """Execute a single tool if its tier is allowed here, else refuse.

        The tier ceiling lets a conversational caller auto-run safe tools while
        refusing destructive ones; a task caller can raise the ceiling.
        """
        if tier > allow_tier:
            return (f"[Refused] '{name}' is a Tier {tier} (high-risk) action that requires "
                    "explicit approval and cannot run from a chat. Ask the user to run it "
                    "as a Task.")
        try:
            from app.services.mcp.tools import run_tool_sync
            return str(run_tool_sync(name, args, session_id=session_id, run_id=run_id))
        except Exception as e:
            return f"Error: {e}"

    # ── Conversational reply with a bounded tool loop ─────────────────────────

    def chat_reply(self, messages: list, max_tool_iters: int = _DEFAULT_MAX_TOOL_ITERS,
                   allow_tier: int = TIER_HARD_STOP - 1,
                   session_id: str | None = None, run_id: str | None = None) -> str:
        """Produce one reply, running a bounded MCP tool-calling loop.

        `messages` is an OpenAI-style history (system + prior turns). Returns the
        agent's final text. Tool calls up to `allow_tier` are executed and fed
        back; higher-tier calls are refused (see run_tool).
        """
        from app.services.llm import build_tool_definitions, retry_with_recovery

        tools = self.tools()
        tool_defs = build_tool_definitions(tools)
        tier_map = {t.name: t.tier for t in tools}

        convo = list(messages)
        dirs_block = self._authorized_dirs_block()
        if dirs_block and convo and convo[0].get("role") == "system":
            convo[0] = {**convo[0], "content": (convo[0].get("content") or "") + dirs_block}

        final_text = ""
        for _ in range(max_tool_iters):
            text, tool_calls, _tok = retry_with_recovery(
                self.base_url, self.api_key, self.model, convo, tool_defs, max_retries=2
            )
            final_text = (text or "").strip()
            if not tool_calls:
                break

            convo.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {"id": tc.get("id", ""), "type": "function",
                     "function": {"name": tc.get("name", ""),
                                  "arguments": json.dumps(tc.get("args", {}) or {})}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                name = tc.get("name", "")
                result = self.run_tool(
                    name, tc.get("args", {}) or {},
                    tier=tier_map.get(name, 0), allow_tier=allow_tier,
                    session_id=session_id, run_id=run_id,
                )
                convo.append({
                    "role": "tool", "tool_call_id": tc.get("id", ""), "content": str(result),
                })
        return final_text
