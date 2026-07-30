"""
Chat Rooms API — group chat spaces for agents and the user.
"""
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from app import db
from app.models.chat_room import ChatRoom, ChatRoomMember, ChatRoomMessage

log = logging.getLogger("orions-belt.rooms")

bp = Blueprint("chat_rooms", __name__, url_prefix="/api/chat-rooms")

# How many prior room messages to feed an agent as conversational context.
_ROOM_HISTORY_LIMIT = 40


def _now():
    return datetime.now(timezone.utc)


def _uuid():
    return str(uuid.uuid4())


# ── Agent replies in rooms ────────────────────────────────────────────────────
#
# A human message starts a bounded "burst" of agent activity. Agents reply, and
# an agent can pull another agent in by @mentioning it — but the whole burst is
# capped so agents can't run away talking to each other. Only human messages
# start a burst; agent messages never trigger a new one on their own.

_MAX_AGENT_TURNS_DEFAULT = 6   # default cap; overridable via admin setting
_MAX_AGENT_TURNS_CEILING = 30  # hard upper bound for the admin setting


def _max_agent_turns() -> int:
    """The admin-configurable cap on agent messages per human message.

    Read from the `agents.max_agent_turns` setting so an administrator can tune
    how much agents talk to each other, clamped to a sane range.
    """
    from app.models.settings import Setting
    try:
        raw = Setting.get("agents.max_agent_turns")
        n = int(raw) if raw not in (None, "") else _MAX_AGENT_TURNS_DEFAULT
    except (ValueError, TypeError):
        n = _MAX_AGENT_TURNS_DEFAULT
    return max(1, min(n, _MAX_AGENT_TURNS_CEILING))


def _post_room_system(room_id: str, text: str):
    """Post a system message into a room (used to surface errors/status)."""
    try:
        db.session.add(ChatRoomMessage(
            id=_uuid(), room_id=room_id, sender_type="system", content=text,
        ))
        room = ChatRoom.query.get(room_id)
        if room:
            room.updated_at = _now()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.warning("failed to post room system message room=%s: %s", room_id, e)


def _normalize_handle(name: str) -> str:
    """A comparable @handle for an agent name: lowercased, spaces→hyphens."""
    return re.sub(r"[^a-z0-9-]", "", (name or "").lower().replace(" ", "-"))


def _mentioned_agents(content: str, agents: list) -> list:
    """Agents explicitly @mentioned in the message (by full handle or first name)."""
    tokens = {t.lower() for t in re.findall(r"@([\w-]+)", content or "")}
    if not tokens:
        return []
    hits = []
    for a in agents:
        handle = _normalize_handle(a.name)
        first = (a.name or "").lower().split(" ")[0] if a.name else ""
        if handle in tokens or (first and first in tokens):
            hits.append(a)
    return hits


def _build_room_history(room_id: str, agent, roster: dict) -> list:
    """Build an OpenAI-style message list for an agent replying in a room.

    The agent's own past messages map to `assistant`; humans map to `user`;
    OTHER agents' messages are user turns prefixed with their name so a
    multi-agent room stays coherent. `roster` maps agent_id → name.
    """
    history = (
        ChatRoomMessage.query.filter_by(room_id=room_id)
        .order_by(ChatRoomMessage.created_at.desc())
        .limit(_ROOM_HISTORY_LIMIT)
        .all()
    )
    history.reverse()

    others = [n for aid, n in roster.items() if aid != agent.id]
    sys = agent.system_prompt or f"You are {agent.name}, a helpful AI assistant."
    sys += (
        f"\n\nYou are '{agent.name}', a participant in a group chat with the user"
        + (f" and other agents: {', '.join(others)}." if others else ".")
        + " Reply conversationally as yourself, in the first person; do not role-play "
        "other participants or prefix your reply with your own name. Keep replies focused. "
    )
    if others:
        sys += (
            "To bring another agent into the discussion, @mention them by name "
            "(e.g. @" + _normalize_handle(others[0]) + "). Only do so when their input is "
            "genuinely needed. If you have nothing to add, reply briefly and stop. "
        )
    sys += "Use the available tools when they help answer or complete the request."

    msgs = [{"role": "system", "content": sys}]
    for m in history:
        if m.sender_type == "system":
            continue
        if m.sender_type == "agent" and m.agent_id == agent.id:
            msgs.append({"role": "assistant", "content": m.content})
        elif m.sender_type == "agent":
            label = roster.get(m.agent_id, "Another agent")
            msgs.append({"role": "user", "content": f"[{label}]: {m.content}"})
        else:  # human
            msgs.append({"role": "user", "content": m.content})
    return msgs


def _generate_agent_reply(agent, provider, room_id, roster) -> str:
    """Produce one agent reply via the shared AgentRuntime (tools + tier gating).

    Tools are the AGENT's (its allowed_tools). Tier 0-2 auto-run; Tier 3
    (destructive) are refused in chat and must go through a Task.
    """
    from app.services.agents.runtime import AgentRuntime
    convo = _build_room_history(room_id, agent, roster)
    return AgentRuntime(agent, provider).chat_reply(convo)


def _run_room_conversation(app, room_id: str, human_content: str):
    """Run one bounded burst of agent activity in response to a human message.

    Runs in a background thread (independent of whatever the user is viewing, so
    the conversation continues even after they switch away). Round 1: the
    @mentioned agents reply, or all agents if none were mentioned. After that,
    agents an earlier reply @mentions are pulled in — capped at _MAX_AGENT_TURNS
    total so agents can't loop forever.
    """
    with app.app_context():
        try:
            from app.models.agent import Agent
            from app.services.agents.runtime import resolve_active_provider

            members = (
                ChatRoomMember.query.filter_by(room_id=room_id)
                .filter(ChatRoomMember.agent_id.isnot(None))
                .all()
            )
            agents = [a for a in (Agent.query.get(m.agent_id) for m in members) if a]
            if not agents:
                return
            roster = {a.id: a.name for a in agents}

            prov = resolve_active_provider()
            # Surface a missing/broken provider in the room instead of failing
            # silently (the #1 reason "the room does nothing").
            if not prov or not prov.get("base_url") or not prov.get("model"):
                _post_room_system(
                    room_id,
                    "⚠ No LLM provider is configured, so agents can't respond. "
                    "Set an active provider in Settings → LLM Provider."
                )
                return

            cap = _max_agent_turns()   # admin-configurable

            queue = list(_mentioned_agents(human_content, agents) or agents)
            queued_ids = {a.id for a in queue}
            turns = 0
            last_id = None

            while queue and turns < cap:
                agent = queue.pop(0)
                queued_ids.discard(agent.id)
                if agent.id == last_id:
                    continue  # no immediate self-reply
                try:
                    reply = _generate_agent_reply(agent, prov, room_id, roster)
                except Exception as e:
                    db.session.rollback()
                    log.warning("room reply failed agent=%s room=%s: %s", agent.id, room_id, e)
                    # Surface the failure in the room so it isn't invisible.
                    _post_room_system(
                        room_id,
                        f"⚠ {agent.name} couldn't respond: {str(e)[:300]}"
                    )
                    continue

                reply = (reply or "").strip()
                if not reply:
                    continue

                db.session.add(ChatRoomMessage(
                    id=_uuid(), room_id=room_id, agent_id=agent.id,
                    sender_type="agent", content=reply,
                ))
                room = ChatRoom.query.get(room_id)
                if room:
                    room.updated_at = _now()
                db.session.commit()

                turns += 1
                last_id = agent.id

                # Cascade: agents THIS reply @mentions (not itself) join the queue.
                for m in _mentioned_agents(reply, agents):
                    if m.id != agent.id and m.id not in queued_ids:
                        queue.append(m)
                        queued_ids.add(m.id)
        except Exception as e:
            log.warning("room conversation failed room=%s: %s", room_id, e)


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
    result = msg.to_dict()

    # A human message triggers the room's agent members to reply (in a
    # background thread so this request returns immediately; the client poll
    # delivers the replies). Agent-authored messages never trigger, so agents
    # can't loop replying to each other.
    if sender_type == "human":
        has_agents = (
            ChatRoomMember.query.filter_by(room_id=room_id)
            .filter(ChatRoomMember.agent_id.isnot(None))
            .first()
        )
        if has_agents:
            app = current_app._get_current_object()
            threading.Thread(
                target=_run_room_conversation,
                args=(app, room_id, content),
                daemon=True,
            ).start()

    return jsonify(result), 201


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


# ── Room Goals ────────────────────────────────────────────────────────────────

@bp.route("/<room_id>/goals", methods=["GET"])
def list_room_goals(room_id):
    from app.models.chat_room_goal import ChatRoomGoal
    goals = ChatRoomGoal.query.filter_by(room_id=room_id).order_by(
        ChatRoomGoal.created_at.desc()
    ).all()
    return jsonify([g.to_dict() for g in goals])


@bp.route("/<room_id>/goals", methods=["POST"])
def create_room_goal(room_id):
    from app.models.chat_room_goal import ChatRoomGoal
    room = ChatRoom.query.get(room_id)
    if not room:
        return jsonify({"error": "Room not found"}), 404
    body = request.get_json() or {}
    goal_text = (body.get("goal_text") or "").strip()
    if not goal_text:
        return jsonify({"error": "goal_text is required"}), 400
    goal = ChatRoomGoal(
        room_id=room_id,
        goal_text=goal_text,
        set_by=body.get("set_by", "user"),
    )
    db.session.add(goal)
    db.session.commit()
    return jsonify(goal.to_dict()), 201


@bp.route("/goals/<goal_id>", methods=["PATCH"])
def update_room_goal(goal_id):
    from app.models.chat_room_goal import ChatRoomGoal
    from datetime import datetime, timezone
    goal = ChatRoomGoal.query.get(goal_id)
    if not goal:
        return jsonify({"error": "Goal not found"}), 404
    body = request.get_json() or {}
    if "goal_text" in body:
        goal.goal_text = body["goal_text"]
    if "status" in body:
        goal.status = body["status"]
        if body["status"] == "completed" and not goal.completed_at:
            goal.completed_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(goal.to_dict())


@bp.route("/goals/<goal_id>", methods=["DELETE"])
def delete_room_goal(goal_id):
    from app.models.chat_room_goal import ChatRoomGoal
    goal = ChatRoomGoal.query.get(goal_id)
    if not goal:
        return jsonify({"error": "Goal not found"}), 404
    db.session.delete(goal)
    db.session.commit()
    return "", 204
