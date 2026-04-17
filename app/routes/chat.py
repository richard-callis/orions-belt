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
from app.services.llm import build_tool_definitions, build_context
from app.services.mcp.tools import execute_tool

bp = Blueprint("chat", __name__, url_prefix="/chat")


def _now():
    return datetime.now(timezone.utc)


def _sse_format(event: str, data: dict | None = None) -> str:
    """Format an SSE event line."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\nretry: 3000\n\n"


def _get_default_system_prompt():
    return (
        "You are Orion's Belt, a local AI assistant running on the user's Windows machine. "
        "You help with project management, coding, data analysis, and executing tasks via MCP tools. "
        "You have access to file operations, SQL queries, REST API calls, and Outlook integration. "
        "Be helpful, concise, and accurate. When executing tools, explain what you're doing."
    )


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
        created_at=_now(),
        updated_at=_now(),
    )
    db.session.add(session)
    db.session.commit()
    return jsonify({
        "id": session.id,
        "name": session.name,
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
    return jsonify([
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
    ])


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

    # Build context for LLM
    strategy = session.context_strategy or "sliding"
    history = build_context(raw_history, strategy=strategy)

    # LLM configuration — read from active provider, fallback to URL overrides
    import json
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
    llm_api_key = (active_provider or {}).get("api_key", Config.LLM_API_KEY)
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
    system_prompt = body.get("system_prompt", _get_default_system_prompt())

    # Load tools
    tools = MCPTool.query.filter_by(enabled=True).all()
    tool_defs = build_tool_definitions(tools)

    # ── PII Guard: scan outbound user message ─────────────────────────────────
    skip_pii = body.get("skip_pii", False)
    if not skip_pii:
        try:
            from app.services.pii_guard import get_pii_guard
            guard = get_pii_guard()
            clean_prompt, pii_found, entity_types = guard.scan(
                prompt, session_id=session_id, direction="outbound"
            )
            if pii_found:
                prompt = clean_prompt  # send sanitized text to LLM
        except Exception:
            pass  # PII guard failure is non-fatal — pass original prompt through

    # ── Memory: inject relevant context into system prompt ────────────────────
    memory_context = ""
    try:
        from app.services.memory import get_memory_service
        mem_svc = get_memory_service()
        memory_context = mem_svc.inject_context(prompt, session_id=session_id)
    except Exception:
        pass  # Memory service failure is non-fatal

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

def _run_tool(tool_name, args):
    """Run an async MCP tool from a synchronous context."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(execute_tool(tool_name, args))
    finally:
        loop.close()


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
                        yield _sse_format("error", {"error": f"API {resp.status_code}: {err}"})
                        llm_log.success = False
                        llm_log.error = err
                        db.session.commit()
                        return

                    for raw_line in resp.iter_lines():
                        # SSE lines arrive as "data: {...}" — strip the prefix
                        line = raw_line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        payload = line[6:]  # strip "data: "
                        if payload in ("[DONE]", "[done]"):
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        choices = chunk.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})

                        content = delta.get("content")
                        if content:
                            turn_text += content
                            total_text += content
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
            if turn_text:
                messages.append({"role": "assistant", "content": turn_text})

            if not pending_tool_calls:
                break  # Final text response — done

            # Execute each tool call
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
                    result = _run_tool(tc["name"], args)
                except Exception as e:
                    result = f"Error: {e}"

                # Persist tool result
                _save_tool_message(session_id, "tool", str(result),
                                   tool_name=tc["name"], tool_call_id=tc["id"])

                yield _sse_format("tool_result", {"tool": tc["name"], "output": str(result)})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })

        except httpx.TimeoutException:
            llm_log.success = False
            llm_log.error = "Request timed out"
            db.session.commit()
            yield _sse_format("error", {"error": "Request timed out"})
            return
        except httpx.ConnectError as e:
            llm_log.success = False
            llm_log.error = f"Connection failed: {e}"
            db.session.commit()
            yield _sse_format("error", {"error": f"Connection failed: {e}"})
            return
        except Exception as e:
            llm_log.success = False
            llm_log.error = str(e)
            db.session.commit()
            yield _sse_format("error", {"error": str(e)})
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
                    result = _run_tool(tc["name"], args)
                except Exception as e:
                    result = f"Error: {e}"

                # Persist tool result
                _save_tool_message(session_id, "tool", str(result),
                                   tool_name=tc["name"], tool_call_id=tc_id)

                yield _sse_format("tool_result", {"tool": tc["name"], "output": str(result)})
                messages.append({"role": "tool", "content": str(result)})

        except httpx.TimeoutException:
            llm_log.success = False
            llm_log.error = "Request timed out"
            db.session.commit()
            yield _sse_format("error", {"error": "Request timed out"})
            return
        except httpx.ConnectError as e:
            llm_log.success = False
            llm_log.error = f"Connection failed: {e}"
            db.session.commit()
            yield _sse_format("error", {"error": f"Connection failed: {e}"})
            return
        except Exception as e:
            llm_log.success = False
            llm_log.error = str(e)
            db.session.commit()
            yield _sse_format("error", {"error": str(e)})
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
    except Exception:
        pass

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
