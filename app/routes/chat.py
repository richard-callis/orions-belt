"""
Orion's Belt — Chat API Routes
Mirrors Orion's chat API pattern with SSE streaming, tool call events, and persistence.

Endpoints:
  GET    /api/sessions              — list sessions
  POST   /api/sessions              — create session
  PATCH  /api/sessions/<id>         — update session
  DELETE /api/sessions/<id>         — archive session
  GET    /api/sessions/<id>/messages — load message history
  POST   /api/sessions/<id>/stream  — SSE chat stream
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger("orions-belt")
from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from app import db
from app.models.chat import Message, Session, ContextCompaction
from app.models.mcp_tool import MCPTool
from app.models.logs import LLMLog, AgentLog
from app.models.settings import Setting
from config import Config
from app.services.llm import build_tool_definitions, build_context, build_context_with_state
from app.services.mcp.tools import execute_tool

bp = Blueprint("chat", __name__, url_prefix="/chat")


def _now():
    return datetime.now(timezone.utc)


def _sse_format(event: str, data: dict | None = None) -> str:
    """Format an SSE event line."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\nretry: 3000\n\n"


_BUILTIN_BASE_PROMPT = (
    "You are Orion's Belt, a local AI assistant running on the user's Windows machine. "
    "You help with project management, coding, data analysis, and executing tasks via MCP tools. "
    "You have access to file operations, SQL queries, REST API calls, and Outlook integration. "
    "Be helpful, concise, and accurate. When executing tools, explain what you're doing."
)

_BUILTIN_PLANNING_SUFFIX = (
    "When in a planning session, ask thoughtful clarifying questions about goals, scope, "
    "constraints, and success criteria. Guide the conversation so that by the end you have "
    "enough detail to write a comprehensive, actionable plan. You may use MCP tools to read "
    "existing files or query data to inform your planning."
)


def _get_base_system_prompt() -> str:
    """Return the configured base system prompt, falling back to the built-in default."""
    from app.models.settings import Setting
    stored = Setting.get("system_prompt.base")
    return stored if stored else _BUILTIN_BASE_PROMPT


def _match_nova_skill(user_message: str) -> str | None:
    """Return injected system prompt if any Nova skill matches the user message."""
    import json
    from app.models.nova import Nova
    msg_lower = user_message.lower()
    skills = Nova.query.filter_by(nova_type="agent").all()
    for nova in skills:
        try:
            config = json.loads(nova.config or "{}")
        except Exception:
            continue
        patterns = config.get("trigger_patterns", [])
        if any(p.lower() in msg_lower for p in patterns):
            injected = config.get("system_prompt", "")
            if injected:
                log.info("nova.skill_injected nova=%s", nova.name)
                return injected
    return None


def _get_planning_suffix() -> str:
    """Return the planning session suffix, falling back to the built-in default."""
    from app.models.settings import Setting
    stored = Setting.get("system_prompt.planning_suffix")
    return stored if stored else _BUILTIN_PLANNING_SUFFIX


def _build_system_prompt(tools: list) -> str:
    """Assemble the full base system prompt with active MCP tools appended.

    In addition to the function-definition objects sent in the request body,
    we embed a text-based invocation format in the system prompt.  This acts
    as a fallback for providers (e.g. Gemini Enterprise) whose OpenAI-compat
    layer silently drops the `tools` field — the model still knows what tools
    exist and how to call them via the <tool_call> block format.
    """
    prompt = _get_base_system_prompt()
    if tools:
        tool_lines = "\n".join(
            f"  - {t.name}: {t.description}" for t in tools
        )
        prompt += f"\n\n## Local MCP Tools\nYou have these tools available:\n{tool_lines}"
        prompt += (
            "\n\n## Calling a tool"
            "\nIf the function-calling mechanism is not available, invoke a tool by"
            " including a call block in your response (on its own line):\n"
            "<tool_call>{\"name\": \"TOOL_NAME\", \"args\": {\"PARAM\": \"VALUE\"}}</tool_call>\n"
            "The system executes the tool and returns the result in the next message.\n"
            "Rules:\n"
            "- Always use tools when the user asks about local files, directories, or data.\n"
            "- Never fabricate file contents — call read_file and use the actual content.\n"
            "- One tool call per block; chain multiple calls across turns if needed.\n"
            "- Do NOT say you cannot access local files — you have tools for this."
        )
    return prompt


# ── Page routes ───────────────────────────────────────────────────────────────

@bp.route("/")
@bp.route("")
def index():
    return render_template("chat.html")


# ── REST API ──────────────────────────────────────────────────────────────────

@bp.route("/api/sessions", methods=["GET"])
def list_sessions():
    sessions = (
        Session.query
        .filter_by(archived=False)
        .order_by(Session.updated_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([
        {
            "id": s.id,
            "name": s.name,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            "message_count": len(s.messages),
            "linked_epic_id": s.linked_epic_id,
            "linked_feature_id": s.linked_feature_id,
            "linked_task_id": s.linked_task_id,
            "context_strategy": s.context_strategy,
        }
        for s in sessions
    ])


@bp.route("/api/sessions", methods=["POST"])
def create_session():
    body = request.get_json() or {}
    session = Session(
        id=str(uuid.uuid4()),
        name=body.get("name", "New conversation"),
        context_strategy=body.get("context_strategy", "sliding"),
        linked_epic_id=body.get("linked_epic_id"),
        linked_feature_id=body.get("linked_feature_id"),
        linked_task_id=body.get("linked_task_id"),
        created_at=_now(),
        updated_at=_now(),
    )
    db.session.add(session)
    db.session.commit()
    return jsonify({
        "id": session.id,
        "name": session.name,
        "linked_epic_id": session.linked_epic_id,
        "linked_feature_id": session.linked_feature_id,
        "linked_task_id": session.linked_task_id,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "message_count": 0,
    }), 201


@bp.route("/api/sessions/<session_id>", methods=["PATCH"])
def update_session(session_id):
    session = Session.query.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    body = request.get_json() or {}
    if "name" in body:
        session.name = body["name"]
        session.updated_at = _now()
    db.session.commit()
    return jsonify({
        "id": session.id,
        "name": session.name,
        "updated_at": session.updated_at.isoformat(),
    })


@bp.route("/api/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id):
    """Soft-delete: mark archived so it disappears from the UI but stays in the DB."""
    session = Session.query.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    session.archived = True
    session.archived_at = _now()
    db.session.commit()
    return "", 204


@bp.route("/api/sessions/<session_id>/save-plan", methods=["POST"])
def save_plan(session_id):
    """Extract a plan description from the session conversation and save it to the linked work item."""
    session = Session.query.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    from app.models.work import Epic, Feature, Task as WorkTask
    if session.linked_task_id:
        item = WorkTask.query.get(session.linked_task_id)
        item_type = "task"
    elif session.linked_feature_id:
        item = Feature.query.get(session.linked_feature_id)
        item_type = "feature"
    elif session.linked_epic_id:
        item = Epic.query.get(session.linked_epic_id)
        item_type = "epic"
    else:
        return jsonify({"error": "This session is not linked to any work item"}), 400

    if not item:
        return jsonify({"error": "Linked work item not found"}), 404

    messages = (
        Message.query.filter_by(session_id=session_id)
        .filter(Message.role.in_(["user", "assistant"]))
        .order_by(Message.created_at.asc())
        .all()
    )
    if not messages:
        return jsonify({"error": "No conversation to save yet"}), 400

    from app.routes.settings import _get_active_provider
    provider = _get_active_provider()
    if not provider:
        return jsonify({"error": "No LLM provider configured"}), 503

    import httpx as _httpx
    base_url = provider["base_url"].rstrip("/")
    api_key = provider.get("api_key", "")
    model = provider["model"]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    title = getattr(item, "title", "") or ""
    conv = "\n".join(f"{m.role.upper()}: {m.content}" for m in messages)
    sys_prompt = "You are a project planning assistant. Write a clear, actionable description."
    user_msg = (
        f'Based on this planning conversation about a {item_type} titled "{title}", '
        "write a concise description (3-5 sentences) capturing the goals, scope, "
        "and key decisions made. Return ONLY the description text, no preamble.\n\n"
        f"Conversation:\n{conv}"
    )

    try:
        with _httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "stream": False,
                },
                headers=headers,
            )
        resp.raise_for_status()
        description = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    item.plan = description
    item.updated_at = _now()
    db.session.commit()
    log.info("save_plan: session=%s item_type=%s id=%s", session_id, item_type,
             session.linked_task_id or session.linked_feature_id or session.linked_epic_id)
    return jsonify({"success": True, "plan": description, "item_type": item_type, "item_title": title})


@bp.route("/api/sessions/<session_id>/messages", methods=["GET", "POST"])
def messages_endpoint(session_id):
    """GET: load message history. POST: append a message (for resilience)."""
    session = Session.query.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    if request.method == "POST":
        body = request.get_json() or {}
        msg = Message(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=body.get("role", "user"),
            content=body.get("content", ""),
            tool_name=body.get("tool_name"),
            tool_call_id=body.get("tool_call_id"),
            created_at=_now(),
            token_count=body.get("token_count"),
        )
        db.session.add(msg)
        db.session.commit()
        return jsonify({
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
        }), 201

    messages = Message.query.filter_by(session_id=session_id)\
        .order_by(Message.created_at.asc())\
        .all()
    compactions = ContextCompaction.query.filter_by(session_id=session_id)\
        .order_by(ContextCompaction.compacted_at.asc())\
        .all()
    return jsonify({
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "token_count": m.token_count,
                "tool_name": m.tool_name,
                "tool_call_id": m.tool_call_id,
            }
            for m in messages
        ],
        "compactions": [
            {
                "id": c.id,
                "messages_compacted": c.messages_compacted,
                "summary": c.summary,
                "archived_messages": json.loads(c.archived_messages) if c.archived_messages else [],
                "timestamp": c.compacted_at.isoformat() if c.compacted_at else None,
            }
            for c in compactions
        ],
    })


# ── SSE Streaming Endpoint ────────────────────────────────────────────────────

@bp.route("/api/sessions/<session_id>/stream", methods=["POST"])
def stream_messages(session_id):
    """SSE streaming chat endpoint.

    Request body:
    {
      "prompt": "user message",
      "model": "gpt-4o",           // optional — uses default
      "base_url": "https://...",   // optional — uses default
      "ollama_model": "llama3",    // optional — for Ollama
      "history_limit": 30,         // optional
      "max_turns": 20,             // optional
    }

    Response: text/event-stream
    Events: text, tool_call, tool_result, done, error
    """
    session = Session.query.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    body = request.get_json() or {}
    prompt = body.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    # Load message history
    # SECURITY: cap history_limit to prevent memory exhaustion
    MAX_HISTORY_LIMIT = 100
    history_limit = min(int(body.get("history_limit", 30)), MAX_HISTORY_LIMIT)

    raw_history = Message.query.filter_by(session_id=session_id)\
        .order_by(Message.created_at.desc())\
        .limit(history_limit)\
        .all()
    raw_history.reverse()  # chronological order

    # Build context for LLM (with compaction state tracking)
    strategy = session.context_strategy or "sliding"
    history, compaction_state = build_context_with_state(
        raw_history, strategy=strategy
    )

    # If compaction is needed, save a compaction record and add summary to history
    _compaction_event = None
    if compaction_state.get("threshold_level") in ("compact", "emergency"):
        # Gather the messages that were compacted (not in history)
        all_llm_ids = {m.id for m in raw_history}
        history_ids = {id(m) for m in raw_history[-history_limit:]}
        compacted_msgs = [
            {"id": m.id, "role": m.role, "content": m.content}
            for m in raw_history
            if m.id not in compaction_state.get("archived_ids", [])
            or compaction_state.get("threshold_level") == "emergency"
        ]
        # If we can't identify compacted messages precisely, use the archived_ids
        if compaction_state.get("archived_ids"):
            compacted_msgs = [
                {"id": m.id, "role": m.role, "content": m.content}
                for m in raw_history
                if m.id in compaction_state["archived_ids"]
            ]
            if not compacted_msgs:
                # Fallback: messages not in the recent window
                compacted_msgs = [
                    {"id": m.id, "role": m.role, "content": m.content}
                    for m in raw_history[:-history_limit]
                ]

        record = ContextCompaction(
            session_id=session_id,
            messages_compacted=compaction_state.get("messages_compacted", len(compacted_msgs)),
            summary=compaction_state.get("summary_text", "[Context compacted]"),
            archived_messages=json.dumps(compacted_msgs[:100]),  # Cap archived messages
        )
        db.session.add(record)
        db.session.commit()

        # Package compaction event for SSE stream
        _compaction_event = {
            "id": record.id,
            "messages_compacted": record.messages_compacted,
            "summary": record.summary,
            "archived_messages": json.loads(record.archived_messages) if record.archived_messages else [],
            "timestamp": record.compacted_at.isoformat() if record.compacted_at else _now().isoformat(),
        }

    # LLM configuration — read from active provider, fallback to URL overrides
    llm_providers_raw = Setting.get("llm.providers")
    llm_active_id = Setting.get("llm.active_provider")

    # Parse providers
    if isinstance(llm_providers_raw, str):
        try:
            llm_providers = json.loads(llm_providers_raw)
        except (json.JSONDecodeError, TypeError):
            llm_providers = []
    else:
        llm_providers = llm_providers_raw or []

    # Find active provider (default to first)
    active_provider = None
    if llm_active_id:
        active_provider = next((p for p in llm_providers if p.get("id") == llm_active_id), None)
    if not active_provider and llm_providers:
        active_provider = llm_providers[0]

    llm_base_url = (active_provider or {}).get("base_url", Config.LLM_BASE_URL)
    raw_key = (active_provider or {}).get("api_key", Config.LLM_API_KEY)
    # Decrypt if it looks like Fernet ciphertext
    _plaintext_prefixes = ("sk-", "sk-proj-", "ghp_", "glpat-", "xoxb-", "xoxp-", "AIza", "EA")
    if raw_key and not raw_key.startswith(_plaintext_prefixes):
        from app.services.crypto import decrypt_data
        raw_key = decrypt_data(raw_key) or raw_key
    llm_api_key = raw_key
    llm_model = (active_provider or {}).get("model", Config.LLM_MODEL)

    explicit_model = body.get("model")
    explicit_base_url = body.get("base_url")
    ollama_model = body.get("ollama_model")

    # Detect provider type.
    # BUG GUARD: "/api" in "https://api.openai.com/v1" is True because "//api"
    # contains "/api" as a substring. Parse the URL and check only the path.
    from urllib.parse import urlparse as _urlparse
    _parsed = _urlparse(llm_base_url or "")
    provider_type = (active_provider or {}).get("type", "genai")
    use_ollama = (
        bool(ollama_model)
        or provider_type == "ollama"
        or "11434" in str(_parsed.port or "")
        or _parsed.path.startswith("/api")
    )
    model = explicit_model or (ollama_model or llm_model)
    base_url = explicit_base_url or llm_base_url
    # Load tools first so the system prompt can include the tool list
    tools = MCPTool.query.filter_by(enabled=True).all()
    tool_defs = build_tool_definitions(tools)

    # Build system prompt: base (from settings or built-in) + active tool list
    system_prompt = body.get("system_prompt") or _build_system_prompt(tools)

    # If the session is linked to a work item, append a planning context block
    if session.linked_task_id or session.linked_feature_id or session.linked_epic_id:
        from app.models.work import Epic, Feature, Task as WorkTask
        if session.linked_task_id:
            _wi = WorkTask.query.get(session.linked_task_id)
            _wi_type = "task"
        elif session.linked_feature_id:
            _wi = Feature.query.get(session.linked_feature_id)
            _wi_type = "feature"
        else:
            _wi = Epic.query.get(session.linked_epic_id)
            _wi_type = "epic"
        if _wi:
            _wi_title = getattr(_wi, "title", "") or ""
            _wi_desc = _wi.description or "(none yet)"
            _planning_suffix = _get_planning_suffix()
            system_prompt += (
                f"\n\n=== PLANNING SESSION ===\n"
                f"You are helping the user plan a {_wi_type}.\n"
                f"  Title: {_wi_title}\n"
                f"  Current description: {_wi_desc}\n\n"
                f"{_planning_suffix}\n"
                f"=== END PLANNING CONTEXT ==="
            )

    # ── PII Guard: scan outbound user message (mandatory) ─────────────────────
    # The skip_pii parameter was removed — PII scanning is now always mandatory.
    # If the guard fails to initialize, the original prompt passes through
    # (non-fatal). This prevents a client from bypassing PII detection.
    pii_globally_enabled = Setting.get("pii.guard.enabled", True)
    _pii_guard_disabled = False  # set True if guard cannot protect this message
    if pii_globally_enabled:
        try:
            from app.services.pii_guard import get_pii_guard
            guard = get_pii_guard()
            clean_prompt, pii_found, entity_types = guard.scan(
                prompt, session_id=session_id, direction="outbound"
            )
            if pii_found:
                prompt = clean_prompt  # send sanitized text to LLM
            if guard.models_unavailable:
                _pii_guard_disabled = True
        except Exception as e:
            log.warning("PII guard failed (non-fatal, original prompt passes through): %s", e)
            _pii_guard_disabled = True

    # ── Nova skill injection — prepend matching skill system prompt ──────────────
    skill_injection = _match_nova_skill(prompt)
    if skill_injection:
        system_prompt = skill_injection + "\n\n" + system_prompt

    # ── Memory: inject relevant context into system prompt ────────────────────
    memory_context = ""
    try:
        from app.services.memory import get_memory_service
        mem_svc = get_memory_service()
        memory_context = mem_svc.inject_context(prompt, session_id=session_id)
    except Exception as e:
        log.warning("Memory service failed (non-fatal): %s", e)

    if memory_context:
        system_prompt = memory_context + "\n\n" + system_prompt

    run_id = str(uuid.uuid4())
    agent_log = AgentLog(
        run_id=run_id, step_number=0, event="started",
        detail=f"Session {session_id} — model: {model}",
    )
    db.session.add(agent_log)
    db.session.commit()

    start = time.time()

    def generate():
        # Emit compaction event first (blue card UI)
        if _compaction_event:
            yield _sse_format("compaction", _compaction_event)

        # Warn when PII guard models are missing — no sensitive data filtering active
        if _pii_guard_disabled:
            yield _sse_format("pii_warning", {
                "message": (
                    "PII guard is not active — the AI models used for sensitive data "
                    "detection are not installed or failed to load. Be cautious about "
                    "what you send to the LLM. Run the model downloader to restore protection."
                )
            })

        # SECURITY: cap max_turns server-side to prevent runaway tool loops.
        # A client or adversarial LLM response cannot exceed MAX_TOOL_TURNS.
        from app.services.mcp.tools import MAX_TOOL_TURNS
        max_turns = min(int(body.get("max_turns", 20)), MAX_TOOL_TURNS)

        # Persist user message before streaming begins
        _save_user_message(session_id, prompt)

        # Prepend user message
        history.append({"role": "user", "content": prompt})

        try:
            if use_ollama:
                yield from _stream_ollama_gen(
                    base_url, model, system_prompt, history,
                    tool_defs, session_id, run_id,
                    max_turns,
                )
            else:
                yield from _stream_openai_gen(
                    base_url, llm_api_key, model, system_prompt, history,
                    tool_defs, session_id, run_id,
                    max_turns,
                )

            # Completion
            duration_ms = int((time.time() - start) * 1000)
            agent_log.event = "completed"
            agent_log.detail = f"Completed in {duration_ms}ms"
            db.session.commit()
            yield _sse_format("done", {})

        except Exception as e:
            agent_log.event = "failed"
            agent_log.detail = str(e)
            db.session.commit()
            yield _sse_format("error", {"error": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Sync stream generators (no async/await for Flask compatibility) ──────────

def _run_tool(tool_name, args, session_id=None, run_id=None):
    """Run an async MCP tool from a synchronous context.

    Args:
        tool_name: Tool to execute.
        args: Tool arguments.
        session_id: Optional session ID for audit trail.
        run_id: Optional run ID for audit trail.
    """
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            execute_tool(tool_name, args, session_id=session_id, run_id=run_id)
        )
    finally:
        loop.close()


import re as _re

def _extract_text_tool_calls(text: str) -> dict:
    """Parse <tool_call>{...}</tool_call> blocks embedded in a text response.

    Used as a fallback when the provider doesn't support native function
    calling (e.g. Gemini Enterprise drops the tools field silently).  The
    model is instructed via the system prompt to use this format instead.

    Returns a pending_tool_calls dict keyed by index, same shape as the
    native tool-call accumulator so the existing execution path handles both.
    """
    calls = {}
    for m in _re.finditer(r'<tool_call>(.*?)</tool_call>', text, _re.DOTALL):
        try:
            payload = json.loads(m.group(1).strip())
            name = payload.get("name", "")
            if not name:
                continue
            idx = len(calls)
            calls[idx] = {
                "id": f"txt_{idx}_{str(uuid.uuid4())[:8]}",
                "name": name,
                "args": json.dumps(payload.get("args", {})),
            }
        except (json.JSONDecodeError, KeyError) as e:
            log.debug("_parse_text_tool_calls: skipping malformed block: %s — %s", m.group(1).strip()[:200], e)
    return calls


def _strip_tool_call_blocks(text: str) -> str:
    """Remove <tool_call>...</tool_call> blocks from text sent back to the LLM.

    The blocks are execution instructions, not conversational content.  Tool
    results come back as separate tool-role messages, so the assistant message
    stored in context should contain only the surrounding prose.
    """
    cleaned = _re.sub(r'\s*<tool_call>.*?</tool_call>\s*', ' ', text, flags=_re.DOTALL)
    return cleaned.strip()


def _stream_openai_gen(base_url, api_key, model, system_prompt, history,
                       tool_defs, session_id, run_id, max_turns):
    """Synchronous OpenAI-compatible streaming with tool loop.

    Uses httpx streaming so text appears token-by-token as it arrives.
    SSE lines from the API arrive as b'data: {...}' — the 'data: ' prefix
    must be stripped before JSON parsing.
    """
    import httpx

    messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    turn_count = 0
    total_text = ""

    while turn_count < max_turns:
        turn_count += 1

        body = {"model": model, "messages": messages, "stream": True}
        if tool_defs:
            body["tools"] = tool_defs
        if "openai" in base_url:
            body["stream_options"] = {"include_usage": True}

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Log the exact outgoing request for debugging auth issues
        masked_key = ("*" * (len(api_key) - 4) + api_key[-4:]) if len(api_key) > 4 else ("*" * len(api_key)) if api_key else "(none)"
        log.info(
            "llm.request  POST %s  model=%s  auth=Bearer %s  messages=%d  stream=True",
            url, model, masked_key, len(messages),
        )

        # Full debug logging — enabled via Settings → LLM → Debug Logging toggle
        from app.models.settings import Setting as _Setting
        _debug_llm = bool(_Setting.get("debug.llm", False))
        if _debug_llm:
            debug_body = {k: v for k, v in body.items() if k != "stream_options"}
            # Mask auth in logged copy
            debug_headers = {k: (f"Bearer {masked_key}" if k == "Authorization" else v)
                             for k, v in headers.items()}
            log.info("llm.debug.request  headers=%s", json.dumps(debug_headers))
            log.info("llm.debug.request  body=%s", json.dumps(debug_body, indent=2, default=str))

        llm_log = LLMLog(
            provider=base_url.split("//")[1].split(":")[0] if "://" in base_url else "custom",
            model=model, session_id=session_id, run_id=run_id,
            tokens_in=0, tokens_out=0, success=True,
        )
        db.session.add(llm_log)

        try:
            turn_text = ""
            pending_tool_calls = {}

            with httpx.Client(timeout=180.0) as client:
                # Use streaming so tokens arrive in real time
                with client.stream("POST", url, json=body, headers=headers) as resp:
                    log.info("llm.response status=%d url=%s", resp.status_code, url)
                    if resp.status_code != 200:
                        err = resp.read().decode()[:500]
                        log.warning("llm.error status=%d body=%s", resp.status_code, err)
                        # Retry transient errors (harness FALLBACK recovery)
                        if resp.status_code in (429, 500, 503) and turn_count < max_turns:
                            backoff = min(2 ** turn_count, 8)
                            log.warning("HTTP %d (turn %d), backing off %ds and retrying",
                                        resp.status_code, turn_count, backoff)
                            time.sleep(backoff)
                            continue
                        yield _sse_format("error", {"error": f"API {resp.status_code}: {err}"})
                        llm_log.success = False
                        llm_log.error = err
                        db.session.commit()
                        return

                    line_count = 0
                    for raw_line in resp.iter_lines():
                        line_count += 1
                        # SSE lines arrive as "data: {...}" — strip the prefix
                        line = raw_line.strip()
                        if not line:
                            continue
                        log.info("llm.raw[%d]: %s", line_count, line if _debug_llm else line[:200])
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]  # strip "data: "
                        if payload in ("[DONE]", "[done]"):
                            log.info("llm.stream done signal received")
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            log.warning("llm.json_error payload=%s", payload[:200])
                            continue

                        # Parse usage chunk (OpenAI sends this as a separate final chunk)
                        usage = chunk.get("usage")
                        if usage:
                            llm_log.tokens_in = usage.get("prompt_tokens", 0)
                            llm_log.tokens_out = usage.get("completion_tokens", 0)

                        choices = chunk.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        finish = choices[0].get("finish_reason")
                        if finish:
                            log.info("llm.finish_reason=%s", finish)

                        content = delta.get("content")
                        if content:
                            turn_text += content
                            total_text += content
                            log.info("llm.text_chunk len=%d total=%d", len(content), len(total_text))
                            yield _sse_format("text", {"content": content})

                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            fn = tc.get("function", {})
                            if idx not in pending_tool_calls:
                                pending_tool_calls[idx] = {
                                    "id": tc.get("id", f"call_{idx}"),
                                    "name": fn.get("name", ""),
                                    "args": "",
                                }
                            if fn.get("name"):
                                pending_tool_calls[idx]["name"] = fn["name"]
                            if fn.get("arguments"):
                                pending_tool_calls[idx]["args"] += fn["arguments"]

            # After stream ends — handle tool calls or break
            log.info("llm.turn_end turn=%d turn_text=%d total_text=%d tool_calls=%d",
                     turn_count, len(turn_text), len(total_text), len(pending_tool_calls))

            # ── Non-streaming fallback ────────────────────────────────────────
            # Some providers (e.g. Gemini Enterprise) return stream=True but
            # immediately emit finish_reason=stop with an empty delta.
            #
            # Fallback 1: retry with stream=False (same body, drops stream_options).
            # Fallback 2: if still empty AND tools were in the request, drop the
            #   tools field and retry.  Gemini Enterprise's OpenAI-compat layer
            #   silently returns empty content when tool definitions are present —
            #   removing them lets the model answer in plain text instead of
            #   trying (and failing) to pick a tool call.
            if not turn_text and not pending_tool_calls:
                log.info("llm.fallback: empty stream, retrying with stream=False")
                fallback_body = {k: v for k, v in body.items() if k != "stream_options"}
                fallback_body["stream"] = False
                fallback_content = ""
                try:
                    with httpx.Client(timeout=180.0) as fb_client:
                        fb_resp = fb_client.post(url, json=fallback_body, headers=headers)
                    log.info("llm.fallback status=%d", fb_resp.status_code)
                    if fb_resp.status_code == 200:
                        fb_data = fb_resp.json()
                        fallback_content = (
                            fb_data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                        ) or ""
                        log.info("llm.fallback content_len=%d", len(fallback_content))
                        if _debug_llm:
                            log.info("llm.debug.fallback  body=%s", json.dumps(fb_data, indent=2, default=str))
                    else:
                        err = fb_resp.text[:300]
                        log.warning("llm.fallback error: %s", err)
                        yield _sse_format("error", {"error": f"API fallback {fb_resp.status_code}: {err}"})
                        return
                except Exception as fb_e:
                    log.error("llm.fallback exception: %s", fb_e)

                # Fallback 2: provider rejected tool definitions — drop them and retry
                if not fallback_content and fallback_body.get("tools"):
                    log.info("llm.fallback2: still empty with tools present, retrying without tools")
                    no_tools_body = {k: v for k, v in fallback_body.items() if k != "tools"}
                    try:
                        with httpx.Client(timeout=180.0) as fb2_client:
                            fb2_resp = fb2_client.post(url, json=no_tools_body, headers=headers)
                        log.info("llm.fallback2 status=%d", fb2_resp.status_code)
                        if fb2_resp.status_code == 200:
                            fb2_data = fb2_resp.json()
                            fallback_content = (
                                fb2_data.get("choices", [{}])[0]
                                .get("message", {})
                                .get("content", "")
                            ) or ""
                            log.info("llm.fallback2 content_len=%d", len(fallback_content))
                            if _debug_llm:
                                log.info("llm.debug.fallback2  body=%s", json.dumps(fb2_data, indent=2, default=str))
                        else:
                            err = fb2_resp.text[:300]
                            log.warning("llm.fallback2 error: %s", err)
                            yield _sse_format("error", {"error": f"API fallback2 {fb2_resp.status_code}: {err}"})
                            return
                    except Exception as fb2_e:
                        log.error("llm.fallback2 exception: %s", fb2_e)

                if fallback_content:
                    turn_text = fallback_content
                    total_text += fallback_content
                    yield _sse_format("text", {"content": fallback_content})

            # ── Text-based tool call fallback ─────────────────────────────────
            # If the provider doesn't support native function calling (e.g.
            # Gemini Enterprise drops the tools field silently), the model
            # may use the <tool_call>{...}</tool_call> format from the system
            # prompt instead.  Parse those blocks and treat them exactly like
            # native tool calls so the same execution path handles both.
            if turn_text and not pending_tool_calls:
                text_calls = _extract_text_tool_calls(turn_text)
                if text_calls:
                    log.info("llm.text_tool_calls found=%d", len(text_calls))
                    pending_tool_calls = text_calls

            # Store the assistant turn; strip <tool_call> blocks from context
            # (the blocks are execution directives, not conversational content —
            # tool results arrive as separate tool-role messages).
            if turn_text:
                clean_turn = _strip_tool_call_blocks(turn_text) if pending_tool_calls else turn_text
                messages.append({"role": "assistant", "content": clean_turn})

            if not pending_tool_calls:
                break  # Final text response — done

            # ── Execute tool calls ────────────────────────────────────────────
            # For native tool calls: append the standard assistant+tool_calls
            # message so the provider tracks the call/result pair correctly.
            # For text-based calls: the assistant message was already appended
            # above (with blocks stripped); no extra tool_calls entry needed.
            is_native = any(not tc["id"].startswith("txt_") for tc in pending_tool_calls.values())
            if is_native:
                messages.append({
                    "role": "assistant",
                    "content": turn_text or None,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc["args"]}}
                        for tc in pending_tool_calls.values()
                    ],
                })

            for tc in pending_tool_calls.values():
                try:
                    args = json.loads(tc["args"]) if tc["args"] else {}
                except json.JSONDecodeError:
                    args = {}

                # Persist tool call invocation
                _save_tool_message(session_id, "tool_call", tc["args"] or "{}",
                                   tool_name=tc["name"], tool_call_id=tc["id"])

                yield _sse_format("tool_call", {"tool": tc["name"], "input": tc["args"]})

                try:
                    result = _run_tool(tc["name"], args, session_id=session_id, run_id=run_id)
                except Exception as e:
                    result = f"Error: {e}"

                # Persist tool result
                _save_tool_message(session_id, "tool", str(result),
                                   tool_name=tc["name"], tool_call_id=tc["id"])

                yield _sse_format("tool_result", {"tool": tc["name"], "output": str(result)})

                # Native tool calls use the `tool` role so the provider can
                # track call/result pairs.  Text-based calls use `user` role
                # because providers that dropped the tools field will reject
                # messages with role="tool" (they don't know about tool calls).
                is_text_call = tc["id"].startswith("txt_")
                if is_text_call:
                    messages.append({
                        "role": "user",
                        "content": f"[Tool result for {tc['name']}]\n{result}",
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })

        except httpx.TimeoutException as e:
            # Transient — retry with backoff (harness FALLBACK recovery)
            llm_log.success = False
            llm_log.error = "Request timed out"
            db.session.commit()
            if turn_count < max_turns:
                log.warning("LLM timeout, backing off and retrying turn %d", turn_count + 1)
                time.sleep(min(2 ** turn_count, 8))
                continue
            yield _sse_format("error", {"error": "Request timed out after retries"})
            return
        except Exception as e:
            err_str = str(e)
            llm_log.success = False
            llm_log.error = err_str
            db.session.commit()
            # Check for recoverable errors (harness FALLBACK recovery)
            if "role" in err_str.lower() and "tool" in err_str.lower():
                # Role ordering error — drop tools and retry
                log.warning("LLM rejected role ordering, dropping tools for turn %d", turn_count + 1)
                tool_defs = []
                continue
            if "too many" in err_str.lower() or "context" in err_str.lower():
                # Context too large — compact messages and retry
                non_system = [m for m in messages if m.get("role") != "system"]
                if len(non_system) > 4:
                    compact_count = len(non_system) // 2
                    messages = (
                        [m for m in messages if m.get("role") == "system"]
                        + [{"role": "system", "content": "[Previous conversation summarized — context truncated]"}]
                        + non_system[-compact_count:]
                    )
                    log.warning("Context too large, compacting for turn %d", turn_count + 1)
                    continue
            yield _sse_format("error", {"error": err_str})
            return

    _save_assistant_message(session_id, total_text)


def _stream_ollama_gen(base_url, model, system_prompt, history,
                       tool_defs, session_id, run_id, max_turns):
    """Synchronous Ollama streaming with tool loop.

    Ollama returns newline-delimited JSON (NDJSON), not SSE.
    Each line is a complete JSON object — do NOT accumulate across lines.
    """
    import httpx

    messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    turn_count = 0
    total_text = ""

    while turn_count < max_turns:
        turn_count += 1

        body = {"model": model, "messages": messages, "stream": True}
        if tool_defs:
            body["tools"] = tool_defs

        url = f"{base_url.rstrip('/')}/api/chat"
        llm_log = LLMLog(
            provider="ollama", model=model,
            session_id=session_id, run_id=run_id,
            tokens_in=0, tokens_out=0, success=True,
        )
        db.session.add(llm_log)

        try:
            turn_text = ""
            pending_tool_calls = {}

            with httpx.Client(timeout=180.0) as client:
                with client.stream("POST", url, json=body) as resp:
                    if resp.status_code != 200:
                        err = resp.read().decode()[:500]
                        # Retry transient errors (harness FALLBACK recovery)
                        if resp.status_code in (429, 500, 503) and turn_count < max_turns:
                            backoff = min(2 ** turn_count, 8)
                            log.warning("Ollama HTTP %d (turn %d), backing off %ds and retrying",
                                        resp.status_code, turn_count, backoff)
                            time.sleep(backoff)
                            continue
                        yield _sse_format("error", {"error": f"Ollama {resp.status_code}: {err}"})
                        llm_log.success = False
                        llm_log.error = err
                        db.session.commit()
                        return

                    for raw_line in resp.iter_lines():
                        # Ollama sends NDJSON — each line is a complete JSON object
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        done = chunk.get("done", False)
                        msg = chunk.get("message", {})

                        content = msg.get("content", "")
                        if content:
                            turn_text += content
                            total_text += content
                            yield _sse_format("text", {"content": content})

                        for tc in msg.get("tool_calls") or []:
                            fn = tc.get("function", {})
                            idx_key = len(pending_tool_calls)
                            args_val = fn.get("arguments", {})
                            pending_tool_calls[idx_key] = {
                                "name": fn.get("name", ""),
                                "args": json.dumps(args_val) if isinstance(args_val, dict) else str(args_val),
                            }

                        if done:
                            break

            # After stream — append assistant message and execute any tool calls
            messages.append({"role": "assistant", "content": turn_text})

            if not pending_tool_calls:
                break

            for tc in pending_tool_calls.values():
                try:
                    args = json.loads(tc["args"]) if tc["args"] else {}
                except json.JSONDecodeError:
                    args = {}

                tc_id = str(uuid.uuid4())[:16]

                # Persist tool call invocation
                _save_tool_message(session_id, "tool_call", tc["args"] or "{}",
                                   tool_name=tc["name"], tool_call_id=tc_id)

                yield _sse_format("tool_call", {"tool": tc["name"], "input": tc["args"]})

                try:
                    result = _run_tool(tc["name"], args, session_id=session_id, run_id=run_id)
                except Exception as e:
                    result = f"Error: {e}"

                # Persist tool result
                _save_tool_message(session_id, "tool", str(result),
                                   tool_name=tc["name"], tool_call_id=tc_id)

                yield _sse_format("tool_result", {"tool": tc["name"], "output": str(result)})
                messages.append({"role": "tool", "content": str(result)})

        except httpx.TimeoutException:
            # Transient — retry with backoff (harness FALLBACK recovery)
            llm_log.success = False
            llm_log.error = "Request timed out"
            db.session.commit()
            if turn_count < max_turns:
                log.warning("LLM timeout (Ollama), backing off and retrying turn %d", turn_count + 1)
                time.sleep(min(2 ** turn_count, 8))
                continue
            yield _sse_format("error", {"error": "Request timed out after retries"})
            return
        except Exception as e:
            err_str = str(e)
            llm_log.success = False
            llm_log.error = err_str
            db.session.commit()
            # Check for recoverable errors (harness FALLBACK recovery)
            if "role" in err_str.lower() and "tool" in err_str.lower():
                log.warning("LLM rejected role ordering (Ollama), dropping tools for turn %d", turn_count + 1)
                tool_defs = []
                continue
            if "too many" in err_str.lower() or "context" in err_str.lower():
                non_system = [m for m in messages if m.get("role") != "system"]
                if len(non_system) > 4:
                    compact_count = len(non_system) // 2
                    messages = (
                        [m for m in messages if m.get("role") == "system"]
                        + [{"role": "system", "content": "[Previous conversation summarized — context truncated]"}]
                        + non_system[-compact_count:]
                    )
                    log.warning("Context too large (Ollama), compacting for turn %d", turn_count + 1)
                    continue
            yield _sse_format("error", {"error": err_str})
            return

    # Persist
    _save_assistant_message(session_id, total_text)


def _save_assistant_message(session_id, content):
    """Save the assistant's response to the database after stream completes."""
    if not content or not content.strip():
        return

    # Restore any PII hash tokens back to original values for storage
    try:
        from app.services.pii_guard import get_pii_guard
        content = get_pii_guard().restore(content)
    except Exception as e:
        log.warning("_save_assistant_message: PII restoration failed, raw tokens will be stored in DB: %s", e)

    msg = Message(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=content[:4000],
        created_at=_now(),
        token_count=len(content) // 4,
    )
    db.session.add(msg)
    db.session.commit()


def _save_user_message(session_id, content):
    """Save the user's message to the database before streaming begins."""
    if not content or not content.strip():
        return
    msg = Message(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="user",
        content=content[:4000],
        created_at=_now(),
        token_count=len(content) // 4,
    )
    db.session.add(msg)
    db.session.commit()


def _save_tool_message(session_id, role, content, tool_name=None, tool_call_id=None):
    """Save a tool_call or tool result message to the database."""
    msg = Message(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role=role,
        content=str(content)[:4000],
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        created_at=_now(),
        token_count=len(str(content)) // 4,
    )
    db.session.add(msg)
    db.session.commit()


# ── Legacy sync generators (for async compat) ─────────────────────────────────

def _stream_openai_with_tools(base_url, api_key, model, system_prompt, history,
                              tool_defs, tool_executor, session_id, run_id, max_turns):
    """Async-compatible wrapper — delegates to the sync generator."""
    for event in _stream_openai_gen(base_url, api_key, model, system_prompt,
                                    history, tool_defs, session_id, run_id, max_turns):
        yield event


def _stream_ollama(base_url, model, system_prompt, history, tool_defs,
                   tool_executor, session_id, run_id, max_turns):
    """Async-compatible wrapper — delegates to the sync generator."""
    for event in _stream_ollama_gen(base_url, model, system_prompt,
                                    history, tool_defs, session_id, run_id, max_turns):
        yield event
