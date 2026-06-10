"""
Orion's Belt — Knowledge Base Routes
CRUD for notes, wikis, runbooks, and llm-context injections.
"""
import logging
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from app import db
from app.models.knowledge import Note

log = logging.getLogger("orions-belt")

bp = Blueprint("knowledge", __name__, url_prefix="/knowledge")


def _now():
    return datetime.now(timezone.utc)


VALID_TYPES = {"note", "wiki", "runbook", "llm-context"}


@bp.route("/api/knowledge", methods=["GET"])
def list_notes():
    q = Note.query
    note_type = request.args.get("type")
    project_id = request.args.get("project_id")
    if note_type:
        q = q.filter_by(note_type=note_type)
    if project_id:
        q = q.filter_by(project_id=project_id)
    notes = q.order_by(Note.pinned.desc(), Note.updated_at.desc()).all()
    return jsonify([n.to_dict() for n in notes])


@bp.route("/api/knowledge", methods=["POST"])
def create_note():
    body = request.get_json() or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    note_type = body.get("note_type", "note")
    if note_type not in VALID_TYPES:
        return jsonify({"error": f"note_type must be one of {sorted(VALID_TYPES)}"}), 400

    note = Note(
        id=str(uuid.uuid4()),
        title=title,
        content=body.get("content", ""),
        note_type=note_type,
        project_id=body.get("project_id"),
        pinned=bool(body.get("pinned", False)),
    )
    db.session.add(note)
    db.session.commit()
    return jsonify(note.to_dict()), 201


@bp.route("/api/knowledge/<note_id>", methods=["GET"])
def get_note(note_id):
    note = Note.query.get(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404
    return jsonify(note.to_dict())


@bp.route("/api/knowledge/<note_id>", methods=["PATCH"])
def update_note(note_id):
    note = Note.query.get(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404
    body = request.get_json() or {}
    if "title" in body:
        note.title = (body["title"] or "").strip() or note.title
    if "content" in body:
        note.content = body["content"]
    if "note_type" in body:
        if body["note_type"] not in VALID_TYPES:
            return jsonify({"error": f"note_type must be one of {sorted(VALID_TYPES)}"}), 400
        note.note_type = body["note_type"]
    if "pinned" in body:
        note.pinned = bool(body["pinned"])
    if "project_id" in body:
        note.project_id = body["project_id"]
    note.updated_at = _now()
    db.session.commit()
    return jsonify(note.to_dict())


@bp.route("/api/knowledge/<note_id>", methods=["DELETE"])
def delete_note(note_id):
    note = Note.query.get(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404
    db.session.delete(note)
    db.session.commit()
    return "", 204
