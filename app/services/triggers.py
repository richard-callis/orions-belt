"""
Scheduled room triggers — periodically checks for due ScheduledTrigger rows
and posts their prompt into the room, same as a human typing it, except
capped at the autonomous Tier-1 tool ceiling (nothing is watching a
scheduled trigger's output the way a human watches a chat reply).
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

log = logging.getLogger("orions-belt.triggers")

_shutdown_event = threading.Event()
_trigger_thread = None

_DEFAULT_TICK_MINUTES = 15
_AUTONOMOUS_ALLOW_TIER = 1  # matches _run_goal_pursuit's AUTONOMOUS_ALLOW_TIER


def _uuid():
    return str(uuid.uuid4())


def _compute_next_run(frequency: str, day_of_week, hour_utc: int, after: datetime | None = None) -> datetime:
    """The next occurrence strictly after `after` (default: now, UTC).

    `hour_utc` is naive-UTC (this app has no per-user timezone concept
    anywhere else either). `day_of_week`: 0=Monday..6=Sunday, only used when
    frequency=="weekly".
    """
    after = after or datetime.now(timezone.utc)
    hour_utc = max(0, min(23, int(hour_utc)))

    candidate = after.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if frequency == "weekly" and day_of_week is not None:
        day_of_week = max(0, min(6, int(day_of_week)))
        days_ahead = (day_of_week - candidate.weekday()) % 7
        candidate = candidate + timedelta(days=days_ahead)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    # daily (or weekly with no day_of_week set — degrade to daily rather than
    # silently never firing)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _dispatch_trigger(app, trigger_id: str, room_id: str, prompt_text: str):
    """Post `prompt_text` into the room exactly like a human message would,
    then dispatch the same goal-pursuit-or-conversation logic post_message
    uses — except _run_room_conversation gets the autonomous Tier-1 ceiling
    explicitly (goal pursuit already always uses Tier 1 internally,
    regardless of caller)."""
    with app.app_context():
        from app import db
        from app.models.chat_room import ChatRoom, ChatRoomMember, ChatRoomMessage
        from app.routes.chat_rooms import (
            _sanitize_for_room, _run_room_conversation, _run_goal_pursuit,
            _bump_goal_generation, _post_room_system,
        )

        room = ChatRoom.query.get(room_id)
        if not room:
            log.warning("Trigger %s: room %s no longer exists", trigger_id, room_id)
            return

        _post_room_system(room_id, "⏰ Scheduled trigger fired")

        content = _sanitize_for_room(prompt_text, room_id)
        db.session.add(ChatRoomMessage(
            id=_uuid(), room_id=room_id, sender_type="human", content=content,
        ))
        room.updated_at = datetime.now(timezone.utc)
        db.session.commit()

        has_agents = (
            ChatRoomMember.query.filter_by(room_id=room_id)
            .filter(ChatRoomMember.agent_id.isnot(None))
            .first()
        )
        if not has_agents:
            _post_room_system(room_id, "⚠ Scheduled trigger fired but this room has no agents to respond.")
            return

        from app.models.chat_room_goal import ChatRoomGoal
        active_goal = ChatRoomGoal.query.filter_by(room_id=room_id, status="active").first()
        if active_goal:
            generation = _bump_goal_generation(active_goal.id)
            _run_goal_pursuit(app, room_id, active_goal.id, generation)
        else:
            _run_room_conversation(app, room_id, content, allow_tier=_AUTONOMOUS_ALLOW_TIER)


def run_due_schedules(model, now: datetime, dispatch_fn, skip_fn=None) -> int:
    """Generic claim-then-dispatch pass over any model with
    enabled/next_run_at/frequency/day_of_week/hour_utc/last_run_at columns
    (ScheduledTrigger and DigestSchedule both share this shape). Shared so
    the actual scheduling behavior — advance next_run_at from NOW, not the
    stale value, so a long-sleeping machine fires once on wake, not once
    per missed slot; commit that advance BEFORE starting any work, so a
    round that runs past the next tick can't be double-fired — has exactly
    one implementation, not one per schedule type.

    `dispatch_fn(row, previous_last_run_at)` does the actual work for one due
    row. `previous_last_run_at` is the row's `last_run_at` value from BEFORE
    this pass claimed it (None if the row has never fired) — callers that
    need "since when" (e.g. a digest's activity window) must use this, not
    `row.last_run_at`, which has already been overwritten to `now` by the
    time dispatch_fn runs. `skip_fn(row)`, if given, can skip dispatch for a
    due row (its next_run_at still advances, same as a dispatch failure)
    without counting as an error — used by triggers for the room-busy check.

    Returns the number of rows actually dispatched (skipped/failed ones
    don't count).
    """
    from app import db

    due = model.query.filter(model.enabled.is_(True), model.next_run_at <= now).all()
    dispatched = 0
    for row in due:
        previous_last_run_at = row.last_run_at
        row.last_run_at = now
        row.next_run_at = _compute_next_run(row.frequency, row.day_of_week, row.hour_utc, after=now)
        db.session.commit()

        if skip_fn and skip_fn(row):
            continue

        try:
            dispatch_fn(row, previous_last_run_at)
            dispatched += 1
        except Exception as e:
            log.error("Schedule %s dispatch failed: %s", row.id, e, exc_info=True)

    return dispatched


def run_due_triggers() -> int:
    """One pass: fire every enabled trigger whose next_run_at has arrived.

    Assumes it is called within a Flask app context — reuses that SAME app
    instance (via current_app) for the dispatched work rather than creating
    a second one, which would use the default (non-test) config and could
    point at a different database than the one this function's own queries
    just ran against.

    Returns the number of triggers actually dispatched (busy-skipped ones
    don't count, but their next_run_at still advances — see the room-busy
    check below).
    """
    from flask import current_app
    from app.models.trigger import ScheduledTrigger
    from app.routes.chat_rooms import _is_room_busy

    now = datetime.now(timezone.utc)
    app = current_app._get_current_object()

    def _dispatch(trig, _previous_last_run_at):
        _dispatch_trigger(app, trig.id, trig.room_id, trig.prompt_text)

    def _skip(trig):
        if _is_room_busy(trig.room_id):
            log.info("Trigger %s: room %s busy — skipping this occurrence", trig.id, trig.room_id)
            return True
        return False

    return run_due_schedules(ScheduledTrigger, now, _dispatch, skip_fn=_skip)


def start_trigger_service(interval_minutes: float = _DEFAULT_TICK_MINUTES) -> None:
    """Start the background trigger-checking thread.

    Same self-contained thread + fresh-app-per-tick pattern as the other
    periodic services (backup/retention/dream) — started from launch.py,
    not app/__init__.py, so it doesn't start inside the test suite or
    Flask's reloader child.
    """
    global _trigger_thread

    def _trigger_loop():
        while not _shutdown_event.is_set():
            try:
                from app import create_app
                app = create_app()
                with app.app_context():
                    run_due_triggers()
            except Exception as e:
                log.error("Trigger thread error: %s", e, exc_info=True)
            _shutdown_event.wait(interval_minutes * 60)
        log.info("Trigger service thread stopped")

    _trigger_thread = threading.Thread(target=_trigger_loop, daemon=True, name="trigger-scheduler")
    _trigger_thread.start()
    log.info("Trigger service started (tick every %.0f min)", interval_minutes)


def stop_trigger_service() -> None:
    """Signal the trigger service thread to stop."""
    global _trigger_thread
    if _trigger_thread and _trigger_thread.is_alive():
        _shutdown_event.set()
        _trigger_thread.join(timeout=30)
        log.info("Trigger service thread stopped")
