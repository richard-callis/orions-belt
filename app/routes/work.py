"""
Orion's Belt — Work Hierarchy Routes
REST API for Projects → Epics → Features → Tasks.
"""
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from app import db
from app.models.work import Epic, Feature, Project, Task
from config import Config

log = logging.getLogger("orions-belt")

bp = Blueprint("work", __name__, url_prefix="/work")


def _now():
    return datetime.now(timezone.utc)


# ── Project folder helpers ────────────────────────────────────────────────────

def _safe_folder_name(name: str) -> str:
    """Convert a project name to a safe directory name."""
    safe = re.sub(r"[^\w\s-]", "", name.lower())
    safe = re.sub(r"[\s_-]+", "-", safe).strip("-")
    return safe or "project"


def _provision_project_folder(project: Project) -> None:
    """Create the project subfolder and register it as an authorized MCP directory.

    Called after the project is added to the session but before commit.
    """
    from app.models.connector import AuthorizedDirectory

    folder_name = _safe_folder_name(project.name)
    projects_root = Config.PROJECTS_DIR
    folder_path = projects_root / folder_name

    # Avoid collisions with existing project folders by appending a short ID
    if folder_path.exists() and not (folder_path / ".project_id").exists():
        folder_path = projects_root / f"{folder_name}-{project.id[:8]}"

    try:
        folder_path.mkdir(parents=True, exist_ok=True)
        (folder_path / ".project_id").write_text(project.id, encoding="utf-8")
    except OSError as e:
        log.error("Failed to create project folder %s: %s", folder_path, e)
        return

    project.folder_path = str(folder_path)
    log.info("project.folder created: %s", folder_path)

    # Register as an authorized directory (upsert by path)
    existing = AuthorizedDirectory.query.filter_by(path=str(folder_path)).first()
    if existing:
        existing.enabled = True
        existing.alias = project.name
    else:
        db.session.add(AuthorizedDirectory(
            path=str(folder_path),
            alias=project.name,
            recursive=True,
            read_only=False,
            max_tier=3,
            enabled=True,
        ))
    log.info("project.mcp_dir registered: alias=%r path=%s", project.name, folder_path)


def _deprovision_project_folder(project: Project) -> None:
    """Disable the MCP directory entry for a deleted project (folder is kept on disk)."""
    from app.models.connector import AuthorizedDirectory

    if not project.folder_path:
        return
    entry = AuthorizedDirectory.query.filter_by(path=project.folder_path).first()
    if entry:
        entry.enabled = False
        log.info("project.mcp_dir disabled: %s", project.folder_path)


# ── Page ──────────────────────────────────────────────────────────────────────

@bp.route("/")
@bp.route("")
def index():
    return render_template("work.html")


# ── Projects ──────────────────────────────────────────────────────────────────

@bp.route("/api/projects", methods=["GET"])
def list_projects():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return jsonify([p.to_dict() for p in projects])


@bp.route("/api/projects", methods=["POST"])
def create_project():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    project = Project(
        id=str(uuid.uuid4()),
        name=name,
        description=body.get("description", ""),
        status=body.get("status", "active"),
    )
    db.session.add(project)
    _provision_project_folder(project)
    db.session.commit()
    log.info("project.created name=%r id=%s folder=%s", name, project.id, project.folder_path)
    return jsonify(project.to_dict()), 201


@bp.route("/api/projects/<project_id>", methods=["GET"])
def get_project(project_id):
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    return jsonify(project.to_dict())


@bp.route("/api/projects/<project_id>", methods=["PATCH"])
def update_project(project_id):
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    body = request.get_json() or {}
    if "name" in body:
        project.name = body["name"]
    if "description" in body:
        project.description = body["description"]
    if "status" in body:
        project.status = body["status"]
    project.updated_at = _now()
    db.session.commit()
    return jsonify(project.to_dict())


@bp.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    _deprovision_project_folder(project)
    db.session.delete(project)
    db.session.commit()
    return "", 204


# ── Epics ─────────────────────────────────────────────────────────────────────

@bp.route("/api/projects/<project_id>/epics", methods=["GET"])
def list_epics(project_id):
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    epics = Epic.query.filter_by(project_id=project_id).order_by(Epic.priority.desc(), Epic.created_at.asc()).all()
    return jsonify([e.to_dict() for e in epics])


@bp.route("/api/projects/<project_id>/epics", methods=["POST"])
def create_epic(project_id):
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    body = request.get_json() or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    epic = Epic(
        id=str(uuid.uuid4()),
        project_id=project_id,
        title=title,
        description=body.get("description", ""),
        status=body.get("status", "backlog"),
        priority=body.get("priority", 0),
    )
    db.session.add(epic)
    db.session.commit()
    return jsonify(epic.to_dict()), 201


@bp.route("/api/epics/<epic_id>", methods=["PATCH"])
def update_epic(epic_id):
    epic = Epic.query.get(epic_id)
    if not epic:
        return jsonify({"error": "Epic not found"}), 404
    body = request.get_json() or {}
    for field in ("title", "description", "status", "priority"):
        if field in body:
            setattr(epic, field, body[field])
    epic.updated_at = _now()
    db.session.commit()
    return jsonify(epic.to_dict())


@bp.route("/api/epics/<epic_id>", methods=["DELETE"])
def delete_epic(epic_id):
    epic = Epic.query.get(epic_id)
    if not epic:
        return jsonify({"error": "Epic not found"}), 404
    db.session.delete(epic)
    db.session.commit()
    return "", 204


# ── Features ──────────────────────────────────────────────────────────────────

@bp.route("/api/epics/<epic_id>/features", methods=["GET"])
def list_features(epic_id):
    epic = Epic.query.get(epic_id)
    if not epic:
        return jsonify({"error": "Epic not found"}), 404
    features = Feature.query.filter_by(epic_id=epic_id).order_by(Feature.priority.desc(), Feature.created_at.asc()).all()
    return jsonify([f.to_dict() for f in features])


@bp.route("/api/epics/<epic_id>/features", methods=["POST"])
def create_feature(epic_id):
    epic = Epic.query.get(epic_id)
    if not epic:
        return jsonify({"error": "Epic not found"}), 404
    body = request.get_json() or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    feature = Feature(
        id=str(uuid.uuid4()),
        epic_id=epic_id,
        title=title,
        description=body.get("description", ""),
        status=body.get("status", "backlog"),
        priority=body.get("priority", 0),
    )
    db.session.add(feature)
    db.session.commit()
    return jsonify(feature.to_dict()), 201


@bp.route("/api/features/<feature_id>", methods=["PATCH"])
def update_feature(feature_id):
    feature = Feature.query.get(feature_id)
    if not feature:
        return jsonify({"error": "Feature not found"}), 404
    body = request.get_json() or {}
    for field in ("title", "description", "status", "priority"):
        if field in body:
            setattr(feature, field, body[field])
    feature.updated_at = _now()
    db.session.commit()
    return jsonify(feature.to_dict())


@bp.route("/api/features/<feature_id>", methods=["DELETE"])
def delete_feature(feature_id):
    feature = Feature.query.get(feature_id)
    if not feature:
        return jsonify({"error": "Feature not found"}), 404
    db.session.delete(feature)
    db.session.commit()
    return "", 204


# ── Tasks ─────────────────────────────────────────────────────────────────────

@bp.route("/api/features/<feature_id>/tasks", methods=["GET"])
def list_tasks(feature_id):
    feature = Feature.query.get(feature_id)
    if not feature:
        return jsonify({"error": "Feature not found"}), 404
    tasks = Task.query.filter_by(feature_id=feature_id).order_by(Task.priority.desc(), Task.created_at.asc()).all()
    return jsonify([t.to_dict() for t in tasks])


@bp.route("/api/features/<feature_id>/tasks", methods=["POST"])
def create_task(feature_id):
    feature = Feature.query.get(feature_id)
    if not feature:
        return jsonify({"error": "Feature not found"}), 404
    body = request.get_json() or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    task = Task(
        id=str(uuid.uuid4()),
        feature_id=feature_id,
        title=title,
        description=body.get("description", ""),
        acceptance_criteria=body.get("acceptance_criteria", ""),
        status=body.get("status", "backlog"),
        priority=body.get("priority", 0),
        assigned_agent_id=body.get("assigned_agent_id"),
    )
    db.session.add(task)
    db.session.commit()
    return jsonify(task.to_dict()), 201


@bp.route("/api/tasks/<task_id>", methods=["GET"])
def get_task(task_id):
    task = Task.query.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task.to_dict())


@bp.route("/api/tasks/<task_id>", methods=["PATCH"])
def update_task(task_id):
    task = Task.query.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    body = request.get_json() or {}
    for field in ("title", "description", "acceptance_criteria", "status", "priority", "assigned_agent_id"):
        if field in body:
            setattr(task, field, body[field])
    task.updated_at = _now()
    db.session.commit()
    return jsonify(task.to_dict())


@bp.route("/api/tasks/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    task = Task.query.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    db.session.delete(task)
    db.session.commit()
    return "", 204
