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


# ── Goal-driven agent pursuit ────────────────────────────────────────────────
#
# Setting a room's goal to "active" triggers its lead agent to autonomously
# work toward it — using tools each round — until it declares the goal met, it
# needs human input, or a bounded round cap is hit. This is a multi-ROUND
# extension of the single-round conversational reply above: each round is one
# AgentRuntime.chat_reply() call (which itself runs a bounded internal tool
# loop), and the round loop re-checks the goal's live status every iteration so
# a human marking it completed/abandoned stops pursuit immediately.

_MAX_GOAL_ROUNDS_DEFAULT = 12   # default cap; overridable via admin setting
_MAX_GOAL_ROUNDS_CEILING = 50   # hard upper bound for the admin setting

_GOAL_COMPLETE_MARKER = "GOAL_COMPLETE"
_HELP_NEEDED_MARKER = "HELP_NEEDED"

# Guards against a duplicate pursuit thread: resuming a goal (or reactivating
# it while an older thread is still mid-round) increments this counter: each
# running thread checks its captured generation against the current one every
# round and exits the moment it's stale. In-memory is fine — this app runs
# single-process (mirrors the JS-side roomSwitchToken pattern in chat.html).
_goal_generation: dict[str, int] = {}


def _bump_goal_generation(goal_id: str) -> int:
    gen = _goal_generation.get(goal_id, 0) + 1
    _goal_generation[goal_id] = gen
    return gen


def _max_goal_rounds() -> int:
    """Admin-configurable cap on rounds spent autonomously pursuing one goal."""
    from app.models.settings import Setting
    try:
        raw = Setting.get("agents.max_goal_rounds")
        n = int(raw) if raw not in (None, "") else _MAX_GOAL_ROUNDS_DEFAULT
    except (ValueError, TypeError):
        n = _MAX_GOAL_ROUNDS_DEFAULT
    return max(1, min(n, _MAX_GOAL_ROUNDS_CEILING))


def _goal_lead_agent(room_id: str, agents: list):
    """The agent that drives goal pursuit: the room's 'lead' member if set,
    else the first agent member. Returns None if the room has no agents."""
    if not agents:
        return None
    members = {
        m.agent_id: m
        for m in ChatRoomMember.query.filter_by(room_id=room_id)
        .filter(ChatRoomMember.agent_id.isnot(None)).all()
    }
    for a in agents:
        m = members.get(a.id)
        if m and m.role == "lead":
            return a
    return agents[0]


def _build_goal_history(room_id: str, agent, roster: dict, goal_text: str,
                        feedback: str | None = None) -> list:
    """Like _build_room_history, but instructs the agent to autonomously pursue
    the given goal (using tools) instead of just replying conversationally.

    `feedback` carries a reviewer's rejection reason from the previous round
    (see _judge_goal_completion) so the agent knows why its "done" claim was
    rejected and what's still missing.
    """
    msgs = _build_room_history(room_id, agent, roster)
    msgs[0]["content"] += (
        f"\n\n## Active goal\nYou are autonomously working to complete this goal:\n"
        f"\"{goal_text}\"\n\n"
        "Use the available tools to make real progress each turn — don't just describe "
        "what you would do. When you believe the goal is FULLY met, end your message with "
        f"the exact line `{_GOAL_COMPLETE_MARKER}` by itself as the LAST line — a reviewer "
        "will independently check your work against the goal before it's accepted, so only "
        "claim this when you're confident. If you are blocked and need the user to answer a "
        "question or make a decision before you can continue, end your message with "
        f"`{_HELP_NEEDED_MARKER}: <your question>` as the LAST line and stop — do not guess."
    )
    if feedback:
        msgs[0]["content"] += (
            f"\n\nA reviewer checked your last completion claim and found it NOT yet met: "
            f"{feedback}\nAddress this before claiming completion again."
        )
    return msgs


def _parse_goal_signal(reply: str) -> tuple[str, str | None, str]:
    """Extract a GOAL_COMPLETE/HELP_NEEDED control signal from an agent reply.

    Anchored to the LAST non-blank line only — a marker mentioned mid-prose
    ("I'll say GOAL_COMPLETE when done") is NOT treated as a signal, and the
    signal line is stripped from what's shown/persisted either way so it can
    never leak into a later round's history as if it were content.

    Returns (shown_text, signal, help_question) where signal is
    "complete" | "help" | None.
    """
    lines = reply.splitlines()
    last_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_idx = i
            break
    if last_idx is None:
        return reply, None, ""

    last_line = lines[last_idx].strip()
    last_upper = last_line.upper()

    if last_upper == _GOAL_COMPLETE_MARKER or last_upper == f"{_GOAL_COMPLETE_MARKER}.":
        shown = "\n".join(lines[:last_idx]).strip()
        return shown, "complete", ""

    if last_upper.startswith(_HELP_NEEDED_MARKER):
        question = last_line[len(_HELP_NEEDED_MARKER):].lstrip(":").strip()
        shown = "\n".join(lines[:last_idx]).strip()
        return shown, "help", question

    return reply, None, ""


def _judge_goal_completion(agent, prov: dict, goal, latest_reply: str) -> tuple[bool, str]:
    """Independent reviewer check before trusting a self-reported GOAL_COMPLETE.

    Mirrors app/services/agents/__init__.py's _run_reviewer pattern (including
    its NOT-APPROVED substring-trap fix): a one-shot judge call, separate from
    the acting agent's own tool loop, using success_criteria when set (else
    the goal text itself). Returns (approved, reason) — reason is empty on
    approval and a short explanation on rejection, fed back into the next
    round via _build_goal_history's `feedback` param.
    """
    from app.services.llm import retry_with_recovery

    criteria = goal.success_criteria or goal.goal_text
    prompt = (
        f"Goal / acceptance criteria:\n{criteria}\n\n"
        f"The agent just reported this as its completion summary:\n{latest_reply}\n\n"
        "Does this genuinely satisfy the goal? Reply with exactly 'APPROVED' if yes, or "
        "'REJECTED: <one short sentence saying what's missing>' if no."
    )
    try:
        resp_text, _tc, _tok = retry_with_recovery(
            prov.get("base_url"), prov.get("api_key"),
            agent.llm_model_override or prov.get("model"),
            [
                {"role": "system", "content": "You are a strict, independent completion reviewer."},
                {"role": "user", "content": prompt},
            ],
            [],
            max_retries=2,
        )
        up = (resp_text or "").strip()
        up_check = up.upper()
        if "REJECT" in up_check or "NOT APPROVED" in up_check:
            reason = up.split(":", 1)[1].strip() if ":" in up else "criteria not met"
            return False, reason or "criteria not met"
        if "APPROVED" in up_check:
            return True, ""
        return False, "reviewer response was inconclusive"
    except Exception as e:
        log.warning("goal reviewer failed: %s", e)
        # Fail CLOSED: a reviewer error must not silently accept an unfinished
        # goal. Worst case here is one extra round (bounded by the round cap
        # and reviewable by a human at any time); the reverse — wrongly
        # marking something complete — is silent and much worse.
        return False, "the completion reviewer hit an error and couldn't verify this"


def _run_goal_pursuit(app, room_id: str, goal_id: str, generation: int):
    """Run bounded autonomous rounds toward a room goal until it's met, the
    agent asks for help, the goal is no longer active, a newer pursuit
    supersedes this one, or the round cap hits.

    Runs in a background thread, independent of what the client is viewing.
    `generation` is captured by the caller (via _bump_goal_generation) BEFORE
    the thread starts, so a resume/reactivate that races with an older
    still-running thread reliably wins — the older thread notices it's stale
    and exits on its next round check.

    Tool calls are capped at Tier 1 (create) here, one notch below the Tier 2
    ceiling attended chat_reply() calls get by default — nothing is watching
    an autonomous round in real time the way a human watches a chat reply, so
    Tier 2 (modify/overwrite) actions are refused just like Tier 3.
    """
    from app.services.agents.runtime import TIER_HARD_STOP
    AUTONOMOUS_ALLOW_TIER = TIER_HARD_STOP - 2  # Tier 1: create-only, unattended

    with app.app_context():
        try:
            from app.models.agent import Agent
            from app.models.chat_room_goal import ChatRoomGoal
            from app.services.agents.runtime import resolve_active_provider

            goal = ChatRoomGoal.query.get(goal_id)
            if not goal or goal.status != "active":
                return
            if _goal_generation.get(goal_id) != generation:
                return  # a newer trigger already superseded this one before it even started

            members = (
                ChatRoomMember.query.filter_by(room_id=room_id)
                .filter(ChatRoomMember.agent_id.isnot(None))
                .all()
            )
            agents = [a for a in (Agent.query.get(m.agent_id) for m in members) if a]
            agent = _goal_lead_agent(room_id, agents)
            if not agent:
                _post_room_system(room_id, "⚠ No agent in this room to pursue the goal. Add one first.")
                return
            roster = {a.id: a.name for a in agents}

            prov = resolve_active_provider()
            if not prov or not prov.get("base_url") or not prov.get("model"):
                _post_room_system(room_id, "⚠ No LLM provider is configured — cannot pursue the goal.")
                return

            cap = _max_goal_rounds()
            _post_room_system(room_id, f"🎯 {agent.name} is working on the goal: {goal.goal_text}")

            from app.services.agents.runtime import AgentRuntime
            runtime = AgentRuntime(agent, prov)
            feedback = None  # reviewer's rejection reason, fed into the next round

            for round_num in range(cap):
                db.session.expire_all()
                goal = ChatRoomGoal.query.get(goal_id)
                if not goal or goal.status != "active":
                    return  # completed/abandoned/deleted elsewhere — stop immediately
                if _goal_generation.get(goal_id) != generation:
                    return  # a newer pursuit for this goal has taken over

                try:
                    convo = _build_goal_history(room_id, agent, roster, goal.goal_text, feedback=feedback)
                    reply = (runtime.chat_reply(convo, allow_tier=AUTONOMOUS_ALLOW_TIER) or "").strip()
                except Exception as e:
                    log.warning("goal pursuit round failed goal=%s room=%s: %s", goal_id, room_id, e)
                    _post_room_system(room_id, f"⚠ {agent.name} hit an error working on the goal: {str(e)[:300]}")
                    return

                if not reply:
                    continue

                shown, signal, help_question = _parse_goal_signal(reply)
                feedback = None  # consumed; only persists across rounds via the return value below

                db.session.add(ChatRoomMessage(
                    id=_uuid(), room_id=room_id, agent_id=agent.id,
                    sender_type="agent", content=shown or reply,
                ))
                room = ChatRoom.query.get(room_id)
                if room:
                    room.updated_at = _now()
                db.session.commit()

                if _goal_generation.get(goal_id) != generation:
                    return  # superseded while we were persisting this round's message

                if signal == "complete":
                    approved, reason = _judge_goal_completion(agent, prov, goal, shown or reply)
                    if approved:
                        goal.status = "completed"
                        goal.completed_at = _now()
                        db.session.commit()
                        _post_room_system(room_id, f"✅ Goal completed: {goal.goal_text}")
                        return
                    # Rejected — loop again next round with the reviewer's reason as feedback.
                    feedback = reason
                    continue

                if signal == "help":
                    # Leave the goal active — a human reply/mention resumes it via the
                    # normal conversational path; pursuit itself stops here.
                    if help_question:
                        _post_room_system(room_id, f"⏸ {agent.name} is waiting on your input to continue.")
                    return

            _post_room_system(
                room_id,
                f"⚠ Reached the round limit ({cap}) while pursuing this goal without "
                "completing it. Send a message to help it continue, or adjust the round "
                "limit in Settings → System Prompts.",
            )
        except Exception as e:
            log.warning("goal pursuit failed goal=%s room=%s: %s", goal_id, room_id, e)


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

    # Scan human messages for PII before they're persisted — the stored text is
    # what gets fed back to agents as room history on every future round, so
    # sanitizing at write time (not just at LLM-call time) keeps PII out of
    # every downstream context, not just the first one. Mirrors the 1:1 chat's
    # outbound-scan behavior (the message the user sees will show [PII:TYPE:…]
    # tokens in place of detected values, same known tradeoff as 1:1 chat).
    if sender_type == "human":
        try:
            from app.services.pii_guard import get_pii_guard
            cleaned, _detected, _types = get_pii_guard().scan(
                content, session_id=room_id, direction="outbound"
            )
            content = cleaned
        except Exception as e:
            log.warning("room PII scan failed room=%s: %s — storing unscanned", room_id, e)

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
    #
    # If the room has an active goal, route to goal pursuit instead of the
    # plain conversational burst — otherwise both would run concurrently
    # against the same room (two threads hitting the LLM in parallel), and a
    # human message is also how a HELP_NEEDED-paused goal resumes.
    if sender_type == "human":
        has_agents = (
            ChatRoomMember.query.filter_by(room_id=room_id)
            .filter(ChatRoomMember.agent_id.isnot(None))
            .first()
        )
        if has_agents:
            from app.models.chat_room_goal import ChatRoomGoal
            active_goal = ChatRoomGoal.query.filter_by(room_id=room_id, status="active").first()
            app = current_app._get_current_object()
            if active_goal:
                generation = _bump_goal_generation(active_goal.id)
                threading.Thread(
                    target=_run_goal_pursuit,
                    args=(app, room_id, active_goal.id, generation),
                    daemon=True,
                ).start()
            else:
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
        success_criteria=(body.get("success_criteria") or "").strip() or None,
        set_by=body.get("set_by", "user"),
    )

    # Only one goal pursues at a time per room — a second active goal would
    # mean two concurrent lead-agent threads interleaving into the same room.
    if goal.status == "active":
        others = ChatRoomGoal.query.filter_by(room_id=room_id, status="active").all()
        for o in others:
            o.status = "abandoned"
            _goal_generation[o.id] = _goal_generation.get(o.id, 0) + 1  # stop its thread
        if others:
            _post_room_system(room_id, "⏹ Superseded by a new goal — previous goal(s) abandoned.")

    db.session.add(goal)
    db.session.commit()

    # A goal defaults to "active" — kick off autonomous pursuit immediately
    # (background thread; independent of what the client is viewing).
    if goal.status == "active":
        generation = _bump_goal_generation(goal.id)
        app = current_app._get_current_object()
        threading.Thread(
            target=_run_goal_pursuit, args=(app, room_id, goal.id, generation), daemon=True,
        ).start()

    return jsonify(goal.to_dict()), 201


@bp.route("/goals/<goal_id>", methods=["PATCH"])
def update_room_goal(goal_id):
    from app.models.chat_room_goal import ChatRoomGoal
    from datetime import datetime, timezone
    goal = ChatRoomGoal.query.get(goal_id)
    if not goal:
        return jsonify({"error": "Goal not found"}), 404
    body = request.get_json() or {}
    was_active = goal.status == "active"
    if "goal_text" in body:
        goal.goal_text = body["goal_text"]
    if "success_criteria" in body:
        goal.success_criteria = (body["success_criteria"] or "").strip() or None
    if "status" in body:
        goal.status = body["status"]
        if body["status"] == "completed" and not goal.completed_at:
            goal.completed_at = datetime.now(timezone.utc)
        if body["status"] != "active":
            # Being paused/abandoned/completed via the API — invalidate any
            # thread still mid-round for this goal so it stops on its next check.
            _goal_generation[goal.id] = _goal_generation.get(goal.id, 0) + 1
    db.session.commit()

    # Resuming a paused/abandoned/completed goal (status flips TO active)
    # restarts pursuit with a fresh generation, which also naturally supersedes
    # any older thread that hadn't noticed the earlier deactivation yet.
    if goal.status == "active" and not was_active:
        others = ChatRoomGoal.query.filter_by(room_id=goal.room_id, status="active").filter(
            ChatRoomGoal.id != goal.id
        ).all()
        for o in others:
            o.status = "abandoned"
            _goal_generation[o.id] = _goal_generation.get(o.id, 0) + 1
        db.session.commit()

        generation = _bump_goal_generation(goal.id)
        app = current_app._get_current_object()
        threading.Thread(
            target=_run_goal_pursuit, args=(app, goal.room_id, goal.id, generation), daemon=True,
        ).start()

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
