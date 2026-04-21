"""
Orion's Belt — Memory Routes
REST API for persistent cross-session memory management.
"""
import logging

from flask import Blueprint, jsonify, render_template, request

log = logging.getLogger("orions-belt")

from app import db
from app.models.memory import Memory

bp = Blueprint("memory", __name__, url_prefix="/memory")


# ── Page ──────────────────────────────────────────────────────────────────────

@bp.route("/")
@bp.route("")
def index():
    return render_template("memory.html")


# ── Memory API ────────────────────────────────────────────────────────────────

@bp.route("/api/memories", methods=["GET"])
def list_memories():
    """List memories with optional type filter."""
    memory_type = request.args.get("type")
    q = Memory.query
    if memory_type:
        q = q.filter_by(memory_type=memory_type)
    memories = q.order_by(Memory.pinned.desc(), Memory.created_at.desc()).limit(200).all()
    return jsonify([m.to_dict() for m in memories])


@bp.route("/api/memories", methods=["POST"])
def create_memory():
    """Store a new memory. Embedding is computed automatically."""
    body = request.get_json() or {}
    title = (body.get("title") or "").strip()
    content = (body.get("content") or "").strip()

    if not title or not content:
        return jsonify({"error": "title and content are required"}), 400

    try:
        from app.services.memory import get_memory_service
        mem_svc = get_memory_service()
        mem = mem_svc.store(
            title=title,
            content=content,
            memory_type=body.get("memory_type", "persistent"),
            scope={
                "project_id": body.get("scope_project_id"),
                "epic_id": body.get("scope_epic_id"),
                "task_id": body.get("scope_task_id"),
                "connector_id": body.get("scope_connector_id"),
            },
            source=body.get("source", "user"),
            pinned=bool(body.get("pinned", False)),
        )
        return jsonify(mem.to_dict()), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/memories/<memory_id>", methods=["GET"])
def get_memory(memory_id):
    mem = Memory.query.get(memory_id)
    if not mem:
        return jsonify({"error": "Memory not found"}), 404
    return jsonify(mem.to_dict())


@bp.route("/api/memories/<memory_id>", methods=["PATCH"])
def update_memory(memory_id):
    mem = Memory.query.get(memory_id)
    if not mem:
        return jsonify({"error": "Memory not found"}), 404
    body = request.get_json() or {}
    if "title" in body:
        mem.title = body["title"]
    if "content" in body:
        mem.content = body["content"]
        # Re-compute embedding when content changes
        try:
            from app.services.memory import get_memory_service
            svc = get_memory_service()
            new_embedding = svc._embed(mem.content)
            if new_embedding:
                mem.embedding = new_embedding
        except Exception as e:
            log.warning("memory update: embedding re-generation failed, search recall may degrade: %s", e)
    if "pinned" in body:
        mem.pinned = bool(body["pinned"])
    if "memory_type" in body:
        mem.memory_type = body["memory_type"]
    from datetime import datetime, timezone
    mem.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(mem.to_dict())


@bp.route("/api/memories/<memory_id>", methods=["DELETE"])
def delete_memory(memory_id):
    mem = Memory.query.get(memory_id)
    if not mem:
        return jsonify({"error": "Memory not found"}), 404
    db.session.delete(mem)
    db.session.commit()
    return "", 204


@bp.route("/api/memories/search", methods=["GET"])
def search_memories():
    """Semantic search across memories using embedding similarity."""
    query = request.args.get("q", "").strip()
    top_k = min(int(request.args.get("k", 10)), 50)

    if not query:
        return jsonify({"error": "q parameter is required"}), 400

    try:
        from app.services.memory import get_memory_service
        mem_svc = get_memory_service()
        results = mem_svc.recall(query, top_k=top_k)
        return jsonify([m.to_dict() for m in results])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
