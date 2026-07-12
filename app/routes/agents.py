"""
Orion's Belt — Agent Management Routes
REST API for agents, runs, and step approval.
"""
import json
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request

from app import db
from app.models.agent import Agent, AgentRun, AgentStep

bp = Blueprint("agents", __name__, url_prefix="/agents")


def _now():
    return datetime.now(timezone.utc)


# ── Page ──────────────────────────────────────────────────────────────────────

@bp.route("/")
@bp.route("")
def index():
    return render_template("agents.html")


# ── Agents CRUD ───────────────────────────────────────────────────────────────

@bp.route("/api/agents", methods=["GET"])
def list_agents():
    agents = Agent.query.order_by(Agent.created_at.desc()).all()
    return jsonify([a.to_dict() for a in agents])


@bp.route("/api/agents", methods=["POST"])
def create_agent():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    allowed_tools = body.get("allowed_tools", [])
    if isinstance(allowed_tools, list):
        allowed_tools_json = json.dumps(allowed_tools)
    else:
        allowed_tools_json = "[]"

    agent = Agent(
        id=str(uuid.uuid4()),
        name=name,
        description=body.get("description", ""),
        system_prompt=body.get("system_prompt", ""),
        allowed_tools=allowed_tools_json,
        llm_model_override=body.get("llm_model_override"),
        max_iterations=min(int(body.get("max_iterations", 20)), 50),
        status="idle",
    )
    db.session.add(agent)
    db.session.commit()
    return jsonify(agent.to_dict()), 201


@bp.route("/api/agents/<agent_id>", methods=["GET"])
def get_agent(agent_id):
    agent = Agent.query.get(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404
    return jsonify(agent.to_dict())


@bp.route("/api/agents/<agent_id>", methods=["PATCH"])
def update_agent(agent_id):
    agent = Agent.query.get(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404
    body = request.get_json() or {}

    if "name" in body:
        agent.name = body["name"]
    if "description" in body:
        agent.description = body["description"]
    if "system_prompt" in body:
        agent.system_prompt = body["system_prompt"]
    if "allowed_tools" in body:
        tools = body["allowed_tools"]
        agent.allowed_tools = json.dumps(tools if isinstance(tools, list) else [])
    if "llm_model_override" in body:
        agent.llm_model_override = body["llm_model_override"] or None
    if "max_iterations" in body:
        agent.max_iterations = min(int(body["max_iterations"]), 50)
    if "daily_token_budget" in body:
        v = body["daily_token_budget"]
        agent.daily_token_budget = int(v) if v is not None else None
    if "monthly_token_budget" in body:
        v = body["monthly_token_budget"]
        agent.monthly_token_budget = int(v) if v is not None else None
    if "role_scope" in body:
        agent.role_scope = body["role_scope"] or None

    agent.updated_at = _now()
    db.session.commit()
    return jsonify(agent.to_dict())


@bp.route("/api/agents/<agent_id>", methods=["DELETE"])
def delete_agent(agent_id):
    agent = Agent.query.get(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404
    db.session.delete(agent)
    db.session.commit()
    return "", 204


# ── Agent Runs ────────────────────────────────────────────────────────────────

@bp.route("/api/agents/<agent_id>/run", methods=["POST"])
def start_run(agent_id):
    """Start an agent run against a task."""
    body = request.get_json() or {}
    task_id = body.get("task_id", "")
    if not task_id:
        return jsonify({"error": "task_id is required"}), 400

    from app.models.work import Task
    task = Task.query.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    agent = Agent.query.get(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    if agent.status == "running":
        return jsonify({"error": "Agent is already running"}), 409

    try:
        from app.services.agents import run_agent
        from flask import current_app
        import threading
        session_id = body.get("session_id")

        # The worker runs outside the request, so it needs its own app context
        # for Flask-SQLAlchemy (Model.query) to work.
        app = current_app._get_current_object()

        run_holder = {}
        error_holder = {}

        def _run():
            with app.app_context():
                try:
                    run = run_agent(agent_id=agent_id, task_id=task_id, session_id=session_id)
                    # Serialize inside the worker's context — the ORM object is
                    # bound to this thread's session and detaches once it ends.
                    run_holder["run"] = run.to_dict() if run else None
                except Exception as e:
                    error_holder["error"] = str(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=2.0)  # wait up to 2s to get initial run record

        if "error" in error_holder:
            return jsonify({"error": error_holder["error"]}), 500
        if run_holder.get("run"):
            return jsonify(run_holder["run"]), 202
        # Still running in background — run_agent commits the run record early,
        # so look it up and return its id so the client can poll/stream it.
        db.session.expire_all()
        recent = (AgentRun.query
                  .filter_by(agent_id=agent_id, task_id=task_id)
                  .order_by(AgentRun.created_at.desc())
                  .first())
        if recent:
            return jsonify(recent.to_dict()), 202
        return jsonify({"status": "accepted", "agent_id": agent_id, "task_id": task_id}), 202
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/tasks", methods=["GET"])
def list_tasks_flat():
    """Flat task search for the agent run picker."""
    from app.models.work import Task
    q_str = (request.args.get("q") or "").strip()
    q = Task.query
    if q_str:
        q = q.filter(Task.title.ilike(f"%{q_str}%"))
    tasks = q.order_by(Task.created_at.desc()).limit(30).all()
    return jsonify([{"id": t.id, "title": t.title, "status": t.status} for t in tasks])


@bp.route("/api/agent-runs", methods=["GET"])
def list_runs():
    """List recent agent runs, optionally filtered by agent_id."""
    agent_id = request.args.get("agent_id")
    q = AgentRun.query
    if agent_id:
        q = q.filter_by(agent_id=agent_id)
    runs = q.order_by(AgentRun.created_at.desc()).limit(50).all()
    return jsonify([r.to_dict() for r in runs])


@bp.route("/api/agent-runs/<run_id>", methods=["GET"])
def get_run(run_id):
    """Get run details including steps."""
    run = AgentRun.query.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    data = run.to_dict()
    from app.models.mcp_tool import MCPTool
    tool_tier = {t.name: t.tier for t in MCPTool.query.all()}
    data["steps"] = [
        {
            "id": s.id,
            "step_number": s.step_number,
            "tool_name": s.tool_name,
            "tier": tool_tier.get(s.tool_name),
            "tool_input": json.loads(s.tool_input) if s.tool_input else None,
            "tool_output": s.tool_output,
            "required_approval": s.required_approval,
            "approved": s.approved,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in run.steps
    ]
    return jsonify(data)


@bp.route("/api/agent-runs/<run_id>/resume-plan", methods=["POST"])
def resume_run_plan(run_id):
    """Approve a pending_validation run and resume execution."""
    body = request.get_json() or {}
    from app.services.agents import approve_plan
    ok = approve_plan(run_id, blocked_steps=body.get("blocked_steps", []))
    if not ok:
        return jsonify({"error": "Run not found or not awaiting plan approval"}), 404
    return jsonify({"success": True, "run_id": run_id})


@bp.route("/api/agent-runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id):
    """Cancel a running or paused agent run."""
    from app.services.agents import cancel_run as svc_cancel
    ok = svc_cancel(run_id)
    if not ok:
        return jsonify({"error": "Run not found"}), 404
    return jsonify({"success": True})


# ── Agent Run Status SSE ──────────────────────────────────────────────────────

@bp.route("/api/agent-runs/<run_id>/stream", methods=["GET"])
def stream_run_status(run_id):
    """SSE stream for agent run status updates.

    Emits JSON events whenever the run's status changes:
    {"status": "running"}
    {"status": "completed"}
    """
    from flask import Response, stream_with_context

    def generate():
        import time
        run = AgentRun.query.get(run_id)
        if not run:
            yield 'data: {"error":"not found"}\n\n'
            return
        prev_status = run.status

        # Emit current status immediately so a client that connects AFTER a
        # transition still renders the correct initial state.
        yield f'data: {{"status":"{run.status}"}}\n\n'
        if run.status in ("completed", "failed", "cancelled"):
            return

        start = time.monotonic()
        MAX_SECONDS = 3600  # safety cap so a stuck run never spins a thread forever

        while time.monotonic() - start < MAX_SECONDS:
            time.sleep(1)  # Check every second
            db.session.expire_all()
            run = AgentRun.query.get(run_id)
            if not run:
                yield 'data: {"error":"gone"}\n\n'
                return
            if run.status != prev_status:
                prev_status = run.status
                yield f'data: {{"status":"{run.status}"}}\n\n'
                # Terminal states — stop streaming
                if run.status in ("completed", "failed", "cancelled"):
                    return
            else:
                # Heartbeat: yielding lets the server observe a client
                # disconnect (GeneratorExit fires at a yield) instead of
                # spinning forever on an idle run, and keeps proxies open.
                yield ': ping\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )# ── Step Approval ─────────────────────────────────────────────────────────────

@bp.route("/api/agent-steps/<step_id>/approve", methods=["POST"])
def approve_step(step_id):
    """Approve or reject a pending Tier 3 agent step."""
    body = request.get_json() or {}
    approved = body.get("approved", True)

    from app.services.agents import approve_step as svc_approve
    ok = svc_approve(step_id, approved=bool(approved))
    if not ok:
        return jsonify({"error": "Step not found"}), 404
    return jsonify({"success": True, "approved": approved})
