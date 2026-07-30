"""
Orion's Belt — Dream review routes.

Pending lessons are never exposed as an MCP tool and approval requires a
human hitting this API (protected by the app's normal auth gate) — an
LLM/agent can never approve its own extracted "lessons" into live memory.
"""
import logging

from flask import Blueprint, jsonify

from app import db
from app.models.dream import DreamLesson

bp = Blueprint("dream", __name__)
log = logging.getLogger("orions-belt")


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


@bp.route("/api/dream/pending", methods=["GET"])
def list_pending_lessons():
    rows = DreamLesson.query.filter_by(status="pending").order_by(DreamLesson.created_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@bp.route("/api/dream/pending/<lesson_id>/approve", methods=["POST"])
def approve_lesson(lesson_id):
    lesson = DreamLesson.query.get(lesson_id)
    if not lesson:
        return jsonify({"error": "Lesson not found"}), 404
    if lesson.status != "pending":
        return jsonify({"error": f"Lesson is already {lesson.status}"}), 409

    from app.services.memory import get_memory_service
    try:
        mem = get_memory_service().store(
            title=lesson.title, content=lesson.content,
            memory_type="lesson", source="dream", pinned=False,
        )
    except Exception as e:
        log.warning("Dream: failed to materialize lesson %s into memory: %s", lesson_id, e)
        return jsonify({"error": f"Failed to store as memory: {e}"}), 500

    lesson.status = "approved"
    lesson.memory_id = mem.id
    lesson.reviewed_at = _now()
    db.session.commit()
    return jsonify(lesson.to_dict())


@bp.route("/api/dream/pending/<lesson_id>/reject", methods=["POST"])
def reject_lesson(lesson_id):
    lesson = DreamLesson.query.get(lesson_id)
    if not lesson:
        return jsonify({"error": "Lesson not found"}), 404
    if lesson.status != "pending":
        return jsonify({"error": f"Lesson is already {lesson.status}"}), 409

    lesson.status = "rejected"
    lesson.reviewed_at = _now()
    db.session.commit()
    return jsonify(lesson.to_dict())


@bp.route("/api/dream/status", methods=["GET"])
def dream_status():
    from app.models.settings import Setting
    return jsonify({
        "enabled": Setting.get("agents.dream_enabled", False) is True,
        "last_extraction_at": Setting.get("dream.last_extraction_at"),
        "pending_count": DreamLesson.query.filter_by(status="pending").count(),
    })
