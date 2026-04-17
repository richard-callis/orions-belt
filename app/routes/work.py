"""
Orion's Belt — Work Hierarchy Routes
REST API for Projects → Epics → Features → Tasks.
"""
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context

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


def _llm_call_text(system_prompt: str, user_prompt: str) -> str:
    """Non-streaming LLM call via the active provider. Returns content string."""
    from app.routes.settings import _get_active_provider
    provider = _get_active_provider()
    if not provider:
        raise RuntimeError("No active LLM provider configured")
    base_url = provider["base_url"].rstrip("/")
    api_key = provider.get("api_key", "")
    model = provider["model"]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
            },
            headers=headers,
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


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
    for field in ("title", "description", "plan", "status", "priority"):
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
    for field in ("title", "description", "plan", "status", "priority"):
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
    for field in ("title", "description", "plan", "acceptance_criteria", "status", "priority", "assigned_agent_id"):
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


# ── AI Planning ───────────────────────────────────────────────────────────────

@bp.route("/api/ai/plan-stream", methods=["POST"])
def plan_stream():
    """SSE streaming planning chat — same format as /chat/api/.../stream."""
    from app.routes.settings import _get_active_provider

    body = request.get_json() or {}
    item_type = body.get("item_type", "item")
    item_title = body.get("item_title", "")
    item_description = body.get("item_description", "")
    messages = body.get("messages", [])  # [{role, content}]

    provider = _get_active_provider()
    if not provider:
        return jsonify({"error": "No active LLM provider configured"}), 503

    system = (
        f"You are an expert project planning assistant helping plan a {item_type}.\n\n"
        f"Current {item_type}:\n"
        f"  Title: {item_title}\n"
        f"  Description: {item_description or '(none yet)'}\n\n"
        "Help the user think through, refine, and plan this item. "
        "Be concise and practical. When asked to write a description or break something down, "
        "be specific and actionable."
    )
    llm_messages = [{"role": "system", "content": system}] + messages

    base_url = provider["base_url"].rstrip("/")
    api_key = provider.get("api_key", "")
    model = provider["model"]

    def _sse(event, data):
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    def _generate():
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            with httpx.Client(timeout=180.0) as client:
                with client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    json={"model": model, "messages": llm_messages, "stream": True},
                    headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        err = resp.read().decode()[:300]
                        yield _sse("error", {"error": err})
                        return
                    full_text = ""
                    for line in resp.iter_lines():
                        if not line or line == "data: [DONE]":
                            continue
                        raw = line[6:] if line.startswith("data: ") else line
                        try:
                            chunk = json.loads(raw)
                            content = (chunk.get("choices", [{}])[0]
                                       .get("delta", {}).get("content") or "")
                            if content:
                                full_text += content
                                yield _sse("text", {"content": content})
                        except Exception:
                            pass

            # Non-streaming fallback for APIs that don't stream (e.g. Gemini Enterprise)
            if not full_text:
                log.info("plan.fallback: retrying with stream=False")
                with httpx.Client(timeout=120.0) as fb:
                    fb_resp = fb.post(
                        f"{base_url}/chat/completions",
                        json={"model": model, "messages": llm_messages, "stream": False},
                        headers=headers,
                    )
                    if fb_resp.status_code == 200:
                        full_text = (fb_resp.json().get("choices", [{}])[0]
                                     .get("message", {}).get("content", ""))
                        if full_text:
                            yield _sse("text", {"content": full_text})

            yield _sse("done", {"full_text": full_text})
        except Exception as e:
            yield _sse("error", {"error": str(e)})

    return Response(
        stream_with_context(_generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/api/ai/apply-plan", methods=["POST"])
def apply_plan():
    """Extract a description from the planning conversation and save it to the item."""
    from app.routes.settings import _get_active_provider

    body = request.get_json() or {}
    item_type = body.get("item_type")
    item_id = body.get("item_id")
    messages = body.get("messages", [])

    model_map = {"epic": Epic, "feature": Feature, "task": Task}
    Model = model_map.get(item_type)
    if not Model or not item_id:
        return jsonify({"error": "item_type must be epic, feature, or task"}), 400

    item = Model.query.get(item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404

    provider = _get_active_provider()
    if not provider:
        return jsonify({"error": "No LLM provider configured"}), 503

    title = getattr(item, "title", "")
    conv = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages if m.get("content")
    )
    system = "You are a project planning assistant. Write a clear, actionable description."
    user_msg = (
        f'Based on this planning conversation about a {item_type} titled "{title}", '
        "write a concise description (3-5 sentences) capturing the goals, scope, "
        "and key decisions. Return ONLY the description text.\n\n"
        f"Conversation:\n{conv}"
    )

    try:
        description = _llm_call_text(system, user_msg).strip()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    item.description = description
    item.updated_at = _now()
    db.session.commit()
    log.info("ai.apply_plan item_type=%s id=%s", item_type, item_id)
    return jsonify({"success": True, "description": description})


@bp.route("/api/ai/generate-features/<epic_id>", methods=["POST"])
def generate_features_ai(epic_id):
    """Auto-generate features for an epic using the active LLM."""
    from app.routes.settings import _get_active_provider

    epic = Epic.query.get(epic_id)
    if not epic:
        return jsonify({"error": "Epic not found"}), 404

    provider = _get_active_provider()
    if not provider:
        return jsonify({"error": "No LLM provider configured"}), 503

    system = "You are a project planning assistant. Respond with ONLY valid JSON — no markdown, no explanation."
    user_msg = (
        f"Break down this epic into 3-6 concrete, deliverable features.\n\n"
        f"Epic title: {epic.title}\n"
        f"Epic description: {epic.description or '(none)'}\n\n"
        'Return a JSON array:\n'
        '[{"title":"...","description":"...","status":"backlog","priority":0}]'
    )

    raw = ""
    try:
        raw = _llm_call_text(system, user_msg)
        match = re.search(r'\[[\s\S]*\]', raw)
        items = json.loads(match.group(0) if match else raw)
    except Exception as e:
        return jsonify({"error": f"Failed to parse LLM response: {e}", "raw": raw[:500]}), 500

    created = []
    for i, item in enumerate(items):
        title = (item.get("title") or "").strip()
        if not title:
            continue
        f = Feature(
            id=str(uuid.uuid4()),
            epic_id=epic_id,
            title=title,
            description=item.get("description", ""),
            status=item.get("status", "backlog"),
            priority=item.get("priority", i),
        )
        db.session.add(f)
        created.append(f)

    db.session.commit()
    log.info("ai.generate_features epic=%s count=%d", epic_id, len(created))
    return jsonify({"created": [f.to_dict() for f in created]}), 201


@bp.route("/api/ai/generate-tasks/<feature_id>", methods=["POST"])
def generate_tasks_ai(feature_id):
    """Auto-generate tasks for a feature using the active LLM."""
    from app.routes.settings import _get_active_provider

    feature = Feature.query.get(feature_id)
    if not feature:
        return jsonify({"error": "Feature not found"}), 404

    provider = _get_active_provider()
    if not provider:
        return jsonify({"error": "No LLM provider configured"}), 503

    system = "You are a project planning assistant. Respond with ONLY valid JSON — no markdown, no explanation."
    user_msg = (
        f"Break down this feature into 3-7 concrete, actionable tasks.\n\n"
        f"Feature title: {feature.title}\n"
        f"Feature description: {feature.description or '(none)'}\n\n"
        'Return a JSON array:\n'
        '[{"title":"...","description":"...","acceptance_criteria":"...","status":"backlog","priority":0}]'
    )

    raw = ""
    try:
        raw = _llm_call_text(system, user_msg)
        match = re.search(r'\[[\s\S]*\]', raw)
        items = json.loads(match.group(0) if match else raw)
    except Exception as e:
        return jsonify({"error": f"Failed to parse LLM response: {e}", "raw": raw[:500]}), 500

    created = []
    for i, item in enumerate(items):
        title = (item.get("title") or "").strip()
        if not title:
            continue
        t = Task(
            id=str(uuid.uuid4()),
            feature_id=feature_id,
            title=title,
            description=item.get("description", ""),
            acceptance_criteria=item.get("acceptance_criteria", ""),
            status=item.get("status", "backlog"),
            priority=item.get("priority", i),
        )
        db.session.add(t)
        created.append(t)

    db.session.commit()
    log.info("ai.generate_tasks feature=%s count=%d", feature_id, len(created))
    return jsonify({"created": [t.to_dict() for t in created]}), 201
