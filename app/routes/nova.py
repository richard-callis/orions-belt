"""
Nova Routes — catalog CRUD + one-click import into Agents / Connectors / MCP Tools / Workflows.
"""
import json
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request

from app import db
from app.models.nova import Nova

bp = Blueprint("nova", __name__, url_prefix="/nova")


def _now():
    return datetime.now(timezone.utc)


def _uuid():
    return str(uuid.uuid4())


# ── Page ──────────────────────────────────────────────────────────────────────

@bp.route("/")
@bp.route("")
def index():
    return render_template("nova.html")


# ── CRUD ──────────────────────────────────────────────────────────────────────

@bp.route("/api/novas", methods=["GET"])
def list_novas():
    q = Nova.query
    nova_type = request.args.get("type")
    category  = request.args.get("category")
    source    = request.args.get("source")
    search    = (request.args.get("q") or "").strip()

    if nova_type:
        q = q.filter_by(nova_type=nova_type)
    if category:
        q = q.filter_by(category=category)
    if source:
        q = q.filter_by(source=source)
    if search:
        like = f"%{search}%"
        q = q.filter(
            db.or_(
                Nova.display_name.ilike(like),
                Nova.description.ilike(like),
                Nova.tags.ilike(like),
            )
        )

    novas = q.order_by(Nova.source.asc(), Nova.display_name.asc()).all()
    return jsonify([n.to_dict() for n in novas])


@bp.route("/api/novas", methods=["POST"])
def create_nova():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip().lower().replace(" ", "_")
    display_name = (body.get("display_name") or body.get("name") or "").strip()
    nova_type = body.get("nova_type", "agent")

    if not name or not display_name:
        return jsonify({"error": "name and display_name are required"}), 400
    if nova_type not in ("agent", "connector", "mcp_tool", "workflow"):
        return jsonify({"error": "invalid nova_type"}), 400
    if Nova.query.filter_by(name=name).first():
        return jsonify({"error": f"Nova '{name}' already exists"}), 409

    nova = Nova(
        id=_uuid(),
        name=name,
        display_name=display_name,
        description=body.get("description", ""),
        nova_type=nova_type,
        category=body.get("category", ""),
        source="user",
        version=body.get("version", "1.0.0"),
        tags=json.dumps(body.get("tags", [])),
        config=json.dumps(body.get("config", {})),
    )
    db.session.add(nova)
    db.session.commit()
    return jsonify(nova.to_dict()), 201


@bp.route("/api/novas/<nova_id>", methods=["GET"])
def get_nova(nova_id):
    nova = Nova.query.get(nova_id)
    if not nova:
        return jsonify({"error": "Nova not found"}), 404
    return jsonify(nova.to_dict())


@bp.route("/api/novas/<nova_id>", methods=["PATCH"])
def update_nova(nova_id):
    nova = Nova.query.get(nova_id)
    if not nova:
        return jsonify({"error": "Nova not found"}), 404
    if nova.source == "bundled":
        return jsonify({"error": "Bundled Novas cannot be edited"}), 403

    body = request.get_json() or {}
    if "display_name" in body:
        nova.display_name = body["display_name"]
    if "description" in body:
        nova.description = body["description"]
    if "category" in body:
        nova.category = body["category"]
    if "tags" in body:
        nova.tags = json.dumps(body["tags"] if isinstance(body["tags"], list) else [])
    if "config" in body:
        nova.config = json.dumps(body["config"] if isinstance(body["config"], dict) else {})
    if "version" in body:
        nova.version = body["version"]

    nova.updated_at = _now()
    db.session.commit()
    return jsonify(nova.to_dict())


@bp.route("/api/novas/<nova_id>", methods=["DELETE"])
def delete_nova(nova_id):
    nova = Nova.query.get(nova_id)
    if not nova:
        return jsonify({"error": "Nova not found"}), 404
    if nova.source == "bundled":
        return jsonify({"error": "Bundled Novas cannot be deleted"}), 403
    db.session.delete(nova)
    db.session.commit()
    return "", 204


# ── Import ────────────────────────────────────────────────────────────────────

@bp.route("/api/novas/<nova_id>/import", methods=["POST"])
def import_nova(nova_id):
    nova = Nova.query.get(nova_id)
    if not nova:
        return jsonify({"error": "Nova not found"}), 404

    body   = request.get_json() or {}
    cfg    = json.loads(nova.config or "{}")

    if nova.nova_type == "agent":
        return _import_agent(nova, cfg, body)
    elif nova.nova_type == "connector":
        return _import_connector(nova, cfg, body)
    elif nova.nova_type == "mcp_tool":
        return _import_mcp_tool(nova, cfg, body)
    elif nova.nova_type == "workflow":
        return _import_workflow(nova, cfg, body)
    else:
        return jsonify({"error": "Unknown nova_type"}), 400


# ── Import helpers ────────────────────────────────────────────────────────────

def _import_agent(nova, cfg, body):
    from app.models.agent import Agent

    name = (body.get("name") or nova.display_name).strip()
    # Deduplicate name
    base, suffix = name, 1
    while Agent.query.filter_by(name=name).first():
        name = f"{base} ({suffix})"
        suffix += 1

    agent = Agent(
        id=_uuid(),
        name=name,
        description=body.get("description") or nova.description or "",
        system_prompt=cfg.get("system_prompt", ""),
        allowed_tools=json.dumps(cfg.get("allowed_tools", [])),
        llm_model_override=cfg.get("llm_model_override"),
        max_iterations=min(int(cfg.get("max_iterations", 20)), 50),
        status="idle",
    )
    db.session.add(agent)
    db.session.commit()
    return jsonify({"created": "agent", "id": agent.id, "name": agent.name}), 201


def _import_connector(nova, cfg, body):
    from app.models.connector import Connector

    name = (body.get("name") or nova.display_name).strip()
    base, suffix = name, 1
    while Connector.query.filter_by(name=name).first():
        name = f"{base} ({suffix})"
        suffix += 1

    connector_cfg = {k: v for k, v in cfg.items()
                     if k not in ("connector_type",)}

    connector = Connector(
        id=_uuid(),
        name=name,
        connector_type=cfg.get("connector_type", "rest_api"),
        description=body.get("description") or nova.description or "",
        config=json.dumps(connector_cfg),
        enabled=True,
    )
    db.session.add(connector)
    db.session.commit()
    return jsonify({"created": "connector", "id": connector.id, "name": connector.name}), 201


def _import_mcp_tool(nova, cfg, body):
    from app.models.mcp_tool import MCPTool

    tools_def = cfg.get("tools", [])
    if not tools_def:
        return jsonify({"error": "Nova has no tool definitions"}), 400

    created = []
    for tool_def in tools_def:
        name = tool_def.get("name", "")
        if not name:
            continue
        # Skip if already exists
        if MCPTool.query.filter_by(name=name).first():
            created.append({"name": name, "skipped": True})
            continue

        tool = MCPTool(
            id=_uuid(),
            name=name,
            description=tool_def.get("description", ""),
            source="nova",
            tier=int(tool_def.get("tier", 1)),
            input_schema=json.dumps(tool_def.get("input_schema", {})),
            enabled=True,
        )
        db.session.add(tool)
        created.append({"name": name, "skipped": False})

    db.session.commit()
    return jsonify({"created": "mcp_tools", "tools": created}), 201


def _import_workflow(nova, cfg, body):
    from app.models.work import Project, Epic, Feature, Task

    project_name = (body.get("name") or nova.display_name).strip()
    base, suffix = project_name, 1
    while Project.query.filter_by(name=project_name).first():
        project_name = f"{base} ({suffix})"
        suffix += 1

    project = Project(
        id=_uuid(),
        name=project_name,
        description=body.get("description") or nova.description or "",
        status="active",
    )
    db.session.add(project)
    db.session.flush()

    for epic_def in cfg.get("epics", []):
        epic = Epic(
            id=_uuid(),
            project_id=project.id,
            title=epic_def.get("title", "Epic"),
            description=epic_def.get("description", ""),
            status="backlog",
        )
        db.session.add(epic)
        db.session.flush()

        for feat_def in epic_def.get("features", []):
            feature = Feature(
                id=_uuid(),
                epic_id=epic.id,
                title=feat_def.get("title", "Feature"),
                description=feat_def.get("description", ""),
                status="backlog",
            )
            db.session.add(feature)
            db.session.flush()

            for task_def in feat_def.get("tasks", []):
                task = Task(
                    id=_uuid(),
                    feature_id=feature.id,
                    title=task_def.get("title", "Task"),
                    description=task_def.get("description", ""),
                    status="backlog",
                )
                db.session.add(task)

    db.session.commit()
    return jsonify({"created": "workflow", "id": project.id, "name": project.name}), 201


# ── Meta: available categories per type ───────────────────────────────────────

@bp.route("/api/novas/meta/categories")
def categories():
    from app.models.nova import (AGENT_CATEGORIES, CONNECTOR_CATEGORIES,
                                  MCP_CATEGORIES, WORKFLOW_CATEGORIES)
    return jsonify({
        "agent":     list(AGENT_CATEGORIES),
        "connector": list(CONNECTOR_CATEGORIES),
        "mcp_tool":  list(MCP_CATEGORIES),
        "workflow":  list(WORKFLOW_CATEGORIES),
    })
