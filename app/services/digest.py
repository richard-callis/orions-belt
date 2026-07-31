"""
Scheduled digest emails — periodically composes a summary of recent agent/
LLM activity (cost, tool calls, pending Dream lessons) and sends it via
send_email. Uses app/models/digest.py::DigestSchedule (a separate table
from ScheduledTrigger — see that model's docstring for why) and
app/services/triggers.py::run_due_schedules for the actual claim-then-
dispatch scheduling, so the scheduling behavior itself has one
implementation, not two.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("orions-belt.digest")

_shutdown_event = threading.Event()
_digest_thread = None

_DEFAULT_TICK_MINUTES = 30
# How far back a digest looks if this is the schedule's first-ever send
# (no last_run_at yet) — a week is a reasonable "catch me up" window
# regardless of whether the schedule itself is daily or weekly.
_DEFAULT_LOOKBACK = timedelta(days=7)


def _compose_digest(period_start: datetime, period_end: datetime) -> tuple[str, str]:
    """Build (plain_text, html) summarizing activity in [period_start, period_end).
    Never raises — a query failure degrades that one section rather than
    failing the whole digest, since a partial summary is still useful and
    "the digest never sends" is a worse failure mode than "one number's
    missing this week."
    """
    from app.models.logs import AuditLog, LLMLog
    from app.models.dream import DreamLesson

    lines = [f"Activity summary: {period_start.date().isoformat()} to {period_end.date().isoformat()}", ""]
    html_rows = []

    try:
        llm_rows = LLMLog.query.filter(
            LLMLog.created_at >= period_start, LLMLog.created_at < period_end,
        ).all()
        total_calls = len(llm_rows)
        total_tokens = sum((r.tokens_in or 0) + (r.tokens_out or 0) for r in llm_rows)
        total_cost = sum(r.estimated_cost_usd or 0 for r in llm_rows)
        failed_calls = sum(1 for r in llm_rows if not r.success)
        lines.append(f"LLM usage: {total_calls} calls, {total_tokens:,} tokens, "
                     f"${total_cost:.2f} estimated cost, {failed_calls} failed")
        html_rows.append(f"<li>LLM usage: {total_calls} calls, {total_tokens:,} tokens, "
                         f"${total_cost:.2f} estimated cost, {failed_calls} failed</li>")
    except Exception as e:
        log.warning("Digest: LLM usage section failed: %s", e)
        lines.append("LLM usage: (unavailable)")
        html_rows.append("<li>LLM usage: (unavailable)</li>")

    try:
        tool_rows = AuditLog.query.filter(
            AuditLog.created_at >= period_start, AuditLog.created_at < period_end,
        ).all()
        total_tools = len(tool_rows)
        by_outcome: dict[str, int] = {}
        for r in tool_rows:
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
        outcome_str = ", ".join(f"{k}={v}" for k, v in sorted(by_outcome.items())) or "none"
        lines.append(f"Tool calls: {total_tools} total ({outcome_str})")
        html_rows.append(f"<li>Tool calls: {total_tools} total ({outcome_str})</li>")
    except Exception as e:
        log.warning("Digest: tool activity section failed: %s", e)
        lines.append("Tool calls: (unavailable)")
        html_rows.append("<li>Tool calls: (unavailable)</li>")

    try:
        pending_lessons = DreamLesson.query.filter_by(status="pending").count()
        lines.append(f"Dream lessons awaiting review: {pending_lessons}")
        html_rows.append(f"<li>Dream lessons awaiting review: {pending_lessons}</li>")
    except Exception as e:
        log.warning("Digest: Dream section failed: %s", e)
        lines.append("Dream lessons awaiting review: (unavailable)")
        html_rows.append("<li>Dream lessons awaiting review: (unavailable)</li>")

    plain = "\n".join(lines)
    html = (f"<h3>Activity summary: {period_start.date().isoformat()} to "
           f"{period_end.date().isoformat()}</h3><ul>" + "".join(html_rows) + "</ul>")
    return plain, html


def _dispatch_digest(schedule_id: str, recipient_email: str, period_start: datetime, period_end: datetime):
    """Compose and send one digest. Runs inside the caller's app context
    (run_due_schedules -> _dispatch, called from run_due_digests, which is
    itself always called within an app context — see run_due_digests).

    Goes through run_tool_sync (the same entry point chat/agent tool calls
    use) rather than calling the send_email handler directly, so this shows
    up in AuditLog like any other tool call instead of being invisible to
    it.
    """
    from app.services.mcp.tools import run_tool_sync

    plain, html = _compose_digest(period_start, period_end)
    subject = f"Orion's Belt activity digest — {period_end.date().isoformat()}"

    try:
        result = run_tool_sync("send_email", {
            "to": recipient_email, "subject": subject, "body": plain, "html": html,
        }, session_id="digest", run_id=schedule_id)
        if result.startswith("Error"):
            log.warning("Digest %s: send_email reported an error: %s", schedule_id, result)
        else:
            log.info("Digest %s: sent to %s", schedule_id, recipient_email)
    except Exception as e:
        log.error("Digest %s: send failed: %s", schedule_id, e, exc_info=True)


def run_due_digests() -> int:
    """One pass: send every enabled digest schedule whose next_run_at has
    arrived. Assumes it is called within a Flask app context — same
    reasoning as triggers.run_due_triggers (reuse the caller's app instance,
    don't create a second one pointed at a possibly-different DB)."""
    from app.models.digest import DigestSchedule
    from app.services.triggers import run_due_schedules

    now = datetime.now(timezone.utc)

    def _dispatch(schedule, previous_last_run_at):
        # Must use the PRE-claim last_run_at run_due_schedules hands us, not
        # schedule.last_run_at — that's already been overwritten to `now` by
        # the time this runs, which would collapse every digest's window to
        # zero length (last_run_at == now == period_end).
        period_start = previous_last_run_at or (now - _DEFAULT_LOOKBACK)
        # last_run_at is naive after a SQLite round-trip (this app's usual
        # datetime-column caveat) — normalize before comparing/subtracting.
        if period_start.tzinfo is None:
            period_start = period_start.replace(tzinfo=timezone.utc)
        _dispatch_digest(schedule.id, schedule.recipient_email, period_start, now)

    return run_due_schedules(DigestSchedule, now, _dispatch)


def start_digest_service(interval_minutes: float = _DEFAULT_TICK_MINUTES) -> None:
    """Start the background digest-checking thread. Same self-contained
    thread + fresh-app-per-tick pattern as the other periodic services
    (backup/retention/dream/triggers) — started from launch.py, never from
    app/__init__.py."""
    global _digest_thread

    def _digest_loop():
        while not _shutdown_event.is_set():
            try:
                from app import create_app
                app = create_app()
                with app.app_context():
                    run_due_digests()
            except Exception as e:
                log.error("Digest thread error: %s", e, exc_info=True)
            _shutdown_event.wait(interval_minutes * 60)
        log.info("Digest service thread stopped")

    _digest_thread = threading.Thread(target=_digest_loop, daemon=True, name="digest-scheduler")
    _digest_thread.start()
    log.info("Digest service started (tick every %.0f min)", interval_minutes)


def stop_digest_service() -> None:
    """Signal the digest service thread to stop."""
    global _digest_thread
    if _digest_thread and _digest_thread.is_alive():
        _shutdown_event.set()
        _digest_thread.join(timeout=30)
        log.info("Digest service thread stopped")
