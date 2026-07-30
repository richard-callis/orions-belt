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

# If the same (tool, args) signature is called this many times within one
# reply's tool loop, stop instead of burning the rest of max_tool_iters on
# a model stuck repeating itself.
_MAX_REPEATED_TOOL_CALLS = 3


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
                   session_id: str | None = None, run_id: str | None = None,
                   tool_log: list | None = None) -> str:
        """Produce one reply, running a bounded MCP tool-calling loop.

        `messages` is an OpenAI-style history (system + prior turns). Returns the
        agent's final text. Tool calls up to `allow_tier` are executed and fed
        back; higher-tier calls are refused (see run_tool).

        If `tool_log` is passed (a list), every tool call made during this reply
        is appended to it as {name, args, tier, result, refused, error} — the
        model's own final text is not a reliable signal that something was
        attempted or failed (it may not mention it at all), so callers that need
        to actually notify a human of tool activity should inspect this rather
        than parse the reply text.

        Enforces the agent's daily/monthly token budget (if any) before each
        LLM call, and stops early if the same tool call repeats — both apply
        here rather than in a caller like _run_goal_pursuit, since tool calls
        happen inside this loop and a caller never sees the individual calls,
        only the final text.
        """
        from app.services.agents import _check_token_budget, _compute_checkpoint_hash, _record_token_usage
        from app.services.llm import build_tool_definitions, retry_with_recovery

        tools = self.tools()
        tool_defs = build_tool_definitions(tools)
        tier_map = {t.name: t.tier for t in tools}

        convo = list(messages)
        dirs_block = self._authorized_dirs_block()
        if dirs_block and convo and convo[0].get("role") == "system":
            convo[0] = {**convo[0], "content": (convo[0].get("content") or "") + dirs_block}

        final_text = ""
        total_tokens = 0
        call_counts: dict[str, int] = {}
        try:
            for _ in range(max_tool_iters):
                budget_error = _check_token_budget(self.agent, pending_tokens=total_tokens)
                if budget_error:
                    final_text = f"[Stopped] {budget_error}"
                    break

                text, tool_calls, tok = retry_with_recovery(
                    self.base_url, self.api_key, self.model, convo, tool_defs, max_retries=2,
                    session_id=session_id, run_id=run_id,
                )
                total_tokens += tok or 0
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
                looping = False
                for tc in tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("args", {}) or {}

                    sig = _compute_checkpoint_hash(name, args)
                    call_counts[sig] = call_counts.get(sig, 0) + 1
                    # Loop detection is checked FIRST, ahead of the
                    # unknown-name refusal below — a model repeating the same
                    # hallucinated name must still trip and stop, rather than
                    # getting individually refused on every one of
                    # max_tool_iters iterations before finally running out.
                    if call_counts[sig] > _MAX_REPEATED_TOOL_CALLS:
                        result = (f"[Refused] '{name}' called with the same arguments "
                                  f"{_MAX_REPEATED_TOOL_CALLS}+ times in a row — stopping to "
                                  "avoid a loop. Try a different approach.")
                        looping = True
                        tier = tier_map.get(name, 0)
                    elif name not in tier_map:
                        # A model can emit a tool name it was never given a
                        # schema for (hallucinated, or named by something else
                        # in context) — defaulting an unknown name to tier 0
                        # would let it slip straight past the tier ceiling
                        # instead of being refused. This agent's tools() call
                        # is the source of truth for what it may use at all.
                        result = f"[Refused] '{name}' is not available to this agent."
                        tier = 0
                    else:
                        tier = tier_map[name]
                        result = self.run_tool(
                            name, args, tier=tier, allow_tier=allow_tier,
                            session_id=session_id, run_id=run_id,
                        )
                    if tool_log is not None:
                        result_str = str(result)
                        tool_log.append({
                            "name": name, "args": args, "tier": tier, "result": result_str,
                            "refused": result_str.startswith("[Refused]"),
                            "error": result_str.startswith("Error"),
                        })
                    convo.append({
                        "role": "tool", "tool_call_id": tc.get("id", ""), "content": str(result),
                    })
                if looping:
                    break
        finally:
            if total_tokens > 0:
                # run_id here is a caller-supplied free-form string (e.g. a
                # room/goal id), not necessarily a real agent_runs.id — never
                # pass it as TokenUsage.run_id, which is a real FK.
                try:
                    _record_token_usage(self.agent.id, None, total_tokens)
                except Exception as e:
                    log.warning("failed to record token usage agent=%s: %s", self.agent.id, e)
        return final_text
