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
        run = run_agent(agent_id=agent_id, task_id=task_id)
        return jsonify(run.to_dict()), 202
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/agent-runs", methods=["GET"])
def list_runs():
    """List recent agent runs."""
    runs = AgentRun.query.order_by(AgentRun.created_at.desc()).limit(50).all()
    return jsonify([r.to_dict() for r in runs])


@bp.route("/api/agent-runs/<run_id>", methods=["GET"])
def get_run(run_id):
    """Get run details including steps."""
    run = AgentRun.query.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    data = run.to_dict()
    data["steps"] = [
        {
            "id": s.id,
            "step_number": s.step_number,
            "tool_name": s.tool_name,
            "tool_input": json.loads(s.tool_input) if s.tool_input else None,
            "tool_output": s.tool_output,
            "required_approval": s.required_approval,
            "approved": s.approved,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in run.steps
    ]
    return jsonify(data)


@bp.route("/api/agent-runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id):
    """Cancel a running or paused agent run."""
    from app.services.agents import cancel_run as svc_cancel
    ok = svc_cancel(run_id)
    if not ok:
        return jsonify({"error": "Run not found"}), 404
    return jsonify({"success": True})


# ── Step Approval ─────────────────────────────────────────────────────────────

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
