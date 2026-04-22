"""
Chat Rooms API — group chat spaces for agents and the user.
"""
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from app import db
from app.models.chat_room import ChatRoom, ChatRoomMember, ChatRoomMessage

bp = Blueprint("chat_rooms", __name__, url_prefix="/api/chat-rooms")


def _now():
    return datetime.now(timezone.utc)


def _uuid():
    return str(uuid.uuid4())


# ── Rooms CRUD ────────────────────────────────────────────────────────────────

@bp.route("", methods=["GET"])
def list_rooms():
    room_type = request.args.get("type")
    q = ChatRoom.query
    if room_type:
        q = q.filter_by(room_type=room_type)
    rooms = q.order_by(ChatRoom.updated_at.desc()).all()
    return jsonify([r.to_dict() for r in rooms])


@bp.route("", methods=["POST"])
def create_room():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    room_type = body.get("room_type", "general")
    if room_type not in ("general", "task", "planning", "ops"):
        room_type = "general"

    room = ChatRoom(
        id=_uuid(),
        name=name,
        description=body.get("description", ""),
        room_type=room_type,
        task_id=body.get("task_id") or None,
    )
    db.session.add(room)
    db.session.flush()

    # Add a system message marking creation
    db.session.add(ChatRoomMessage(
        id=_uuid(), room_id=room.id,
        sender_type="system",
        content=f'Room "{name}" created.',
    ))

    # Add any initial agent members
    for agent_id in body.get("agent_ids", []):
        from app.models.agent import Agent
        if Agent.query.get(agent_id):
            db.session.add(ChatRoomMember(
                id=_uuid(), room_id=room.id, agent_id=agent_id, role="member",
            ))
            db.session.add(ChatRoomMessage(
                id=_uuid(), room_id=room.id, sender_type="system",
                content=f"Agent joined the room.",
            ))

    db.session.commit()
    return jsonify(room.to_dict()), 201


@bp.route("/<room_id>", methods=["GET"])
def get_room(room_id):
    room = ChatRoom.query.get(room_id)
    if not room:
        return jsonify({"error": "Room not found"}), 404
    return jsonify(room.to_dict(include_messages=True))


@bp.route("/<room_id>", methods=["PATCH"])
def update_room(room_id):
    room = ChatRoom.query.get(room_id)
    if not room:
        return jsonify({"error": "Room not found"}), 404
    body = request.get_json() or {}
    if "name" in body:
        room.name = body["name"]
    if "description" in body:
        room.description = body["description"]
    if "room_type" in body and body["room_type"] in ("general", "task", "planning", "ops"):
        room.room_type = body["room_type"]
    room.updated_at = _now()
    db.session.commit()
    return jsonify(room.to_dict())


@bp.route("/<room_id>", methods=["DELETE"])
def delete_room(room_id):
    room = ChatRoom.query.get(room_id)
    if not room:
        return jsonify({"error": "Room not found"}), 404
    db.session.delete(room)
    db.session.commit()
    return "", 204


# ── Messages ──────────────────────────────────────────────────────────────────

@bp.route("/<room_id>/messages", methods=["POST"])
def post_message(room_id):
    room = ChatRoom.query.get(room_id)
    if not room:
        return jsonify({"error": "Room not found"}), 404

    body = request.get_json() or {}
    content = (body.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400

    agent_id    = body.get("agent_id") or None
    sender_type = "agent" if agent_id else "human"

    msg = ChatRoomMessage(
        id=_uuid(),
        room_id=room_id,
        agent_id=agent_id,
        sender_type=sender_type,
        content=content,
    )
    db.session.add(msg)
    room.updated_at = _now()
    db.session.commit()
    return jsonify(msg.to_dict()), 201


@bp.route("/<room_id>/messages", methods=["GET"])
def list_messages(room_id):
    room = ChatRoom.query.get(room_id)
    if not room:
        return jsonify({"error": "Room not found"}), 404
    limit  = min(int(request.args.get("limit", 100)), 200)
    after  = request.args.get("after")  # ISO timestamp — for polling new messages

    q = ChatRoomMessage.query.filter_by(room_id=room_id)
    if after:
        from datetime import datetime
        try:
            ts = datetime.fromisoformat(after.replace("Z", "+00:00"))
            q = q.filter(ChatRoomMessage.created_at > ts)
        except ValueError:
            pass
    msgs = q.order_by(ChatRoomMessage.created_at.desc()).limit(limit).all()
    return jsonify([m.to_dict() for m in reversed(msgs)])


# ── Members ───────────────────────────────────────────────────────────────────

@bp.route("/<room_id>/members", methods=["POST"])
def add_member(room_id):
    room = ChatRoom.query.get(room_id)
    if not room:
        return jsonify({"error": "Room not found"}), 404

    body     = request.get_json() or {}
    agent_id = body.get("agent_id")
    if not agent_id:
        return jsonify({"error": "agent_id is required"}), 400

    from app.models.agent import Agent
    agent = Agent.query.get(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    existing = ChatRoomMember.query.filter_by(room_id=room_id, agent_id=agent_id).first()
    if existing:
        return jsonify({"error": "Agent is already a member"}), 409

    member = ChatRoomMember(
        id=_uuid(), room_id=room_id, agent_id=agent_id,
        role=body.get("role", "member"),
    )
    db.session.add(member)
    db.session.add(ChatRoomMessage(
        id=_uuid(), room_id=room_id, sender_type="system",
        content=f'Agent "{agent.name}" joined the room.',
    ))
    room.updated_at = _now()
    db.session.commit()
    return jsonify(member.to_dict()), 201


@bp.route("/<room_id>/members/<agent_id>", methods=["DELETE"])
def remove_member(room_id, agent_id):
    member = ChatRoomMember.query.filter_by(room_id=room_id, agent_id=agent_id).first()
    if not member:
        return jsonify({"error": "Member not found"}), 404

    from app.models.agent import Agent
    agent = Agent.query.get(agent_id)
    name  = agent.name if agent else "Agent"

    db.session.delete(member)
    room = ChatRoom.query.get(room_id)
    if room:
        db.session.add(ChatRoomMessage(
            id=_uuid(), room_id=room_id, sender_type="system",
            content=f'Agent "{name}" left the room.',
        ))
        room.updated_at = _now()
    db.session.commit()
    return "", 204
