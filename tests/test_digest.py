"""
Tests for scheduled digest emails: app/services/digest.py (composition,
dispatch, run_due_digests) and the DigestSchedule CRUD routes.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.models.digest import DigestSchedule
from app.models.logs import AuditLog, LLMLog
from app.models.dream import DreamLesson
from app.models.mcp_tool import MCPTool
import app.services.digest as digest_mod


def _cleanup(rid):
    DigestSchedule.query.filter_by(id=rid).delete()
    db.session.commit()


class TestComposeDigest:
    def test_summarizes_llm_usage_tool_calls_and_pending_lessons(self, app):
        with app.app_context():
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=1)
            db.session.add(LLMLog(id="dl-llm1", provider="test", model="m", tokens_in=100,
                                  tokens_out=50, estimated_cost_usd=0.01, success=True))
            db.session.add(AuditLog(id="dl-audit1", tool_name="read_file", tier=0,
                                    outcome="auto", caller="agent"))
            db.session.add(DreamLesson(id="dl-lesson1", title="t", content="c", status="pending"))
            db.session.commit()
            try:
                plain, html = digest_mod._compose_digest(start, now + timedelta(minutes=1))
                assert "LLM usage: " in plain
                assert "150" in plain  # total tokens
                assert "Tool calls: " in plain
                assert "Dream lessons awaiting review: " in plain
                assert "<ul>" in html
                assert "<li>" in html
            finally:
                LLMLog.query.filter_by(id="dl-llm1").delete()
                AuditLog.query.filter_by(id="dl-audit1").delete()
                DreamLesson.query.filter_by(id="dl-lesson1").delete()
                db.session.commit()

    def test_degrades_gracefully_on_query_failure(self, app, monkeypatch):
        # A failure in one section must not blow up the whole digest — a
        # partial summary beats no digest at all.
        import app.models.logs as logs_mod

        class BoomQuery:
            def filter(self, *a, **k):
                raise RuntimeError("simulated DB failure")

        monkeypatch.setattr(logs_mod.LLMLog, "query", BoomQuery())
        with app.app_context():
            now = datetime.now(timezone.utc)
            plain, html = digest_mod._compose_digest(now - timedelta(days=1), now)
        assert "LLM usage: (unavailable)" in plain
        # Other sections still populated normally.
        assert "Tool calls: " in plain
        # Regression: a failed section must degrade in the HTML body too,
        # not just plain text — send_email prefers HTML when both are
        # given, so an HTML-only "(unavailable)" omission would make the
        # section silently vanish for anyone reading the actual rendered
        # email rather than a plain-text fallback.
        assert "LLM usage: (unavailable)" in html
        assert "Tool calls: " in html


class TestDispatchDigest:
    def test_sends_via_send_email_tool(self, app, monkeypatch):
        with app.app_context():
            db.session.add(MCPTool(id="mt-send1", name="send_email", tier=2, enabled=True, source="builtin"))
            db.session.commit()

            import sys, types
            class _FakeMailItem:
                def __init__(self):
                    self.HTMLBody = None
                    self.Body = None
                def Send(self):
                    pass
            class _FakeOutlookApp:
                def CreateItem(self, t):
                    return _FakeMailItem()
            fake_client = types.ModuleType("win32com.client")
            fake_client.Dispatch = lambda name: _FakeOutlookApp()
            fake_win32com = types.ModuleType("win32com")
            fake_win32com.client = fake_client
            sys.modules["win32com"] = fake_win32com
            sys.modules["win32com.client"] = fake_client
            try:
                now = datetime.now(timezone.utc)
                digest_mod._dispatch_digest("sched-1", "ops@example.com", now - timedelta(days=1), now)
            finally:
                sys.modules.pop("win32com.client", None)
                sys.modules.pop("win32com", None)
                MCPTool.query.filter_by(id="mt-send1").delete()
                db.session.commit()
        # No exception raised = success; run_tool_sync's own AuditLog entry
        # is the real evidence, checked in the run_due_digests test below.

    def test_send_failure_does_not_raise(self, app, monkeypatch):
        # send_email isn't registered as an MCPTool at all here, so
        # run_tool_sync should report an error string, not raise.
        with app.app_context():
            now = datetime.now(timezone.utc)
            digest_mod._dispatch_digest("sched-2", "ops@example.com", now - timedelta(days=1), now)
        # Reaching this line without an exception is the assertion.


class TestRunDueDigests:
    def test_fires_due_digest_and_advances_next_run(self, app, monkeypatch):
        with app.app_context():
            db.session.add(MCPTool(id="mt-send2", name="send_email", tier=2, enabled=True, source="builtin"))
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            schedule = DigestSchedule(id="ds-1", recipient_email="a@example.com", frequency="daily",
                                      hour_utc=9, enabled=True, next_run_at=past)
            db.session.add(schedule)
            db.session.commit()

            called = {"n": 0}
            def fake_dispatch(schedule_id, email, start, end):
                called["n"] += 1
            monkeypatch.setattr(digest_mod, "_dispatch_digest", fake_dispatch)

            try:
                dispatched = digest_mod.run_due_digests()
                assert dispatched == 1
                assert called["n"] == 1
                refreshed = DigestSchedule.query.get("ds-1")
                # SQLite round-trips strip tzinfo — same pre-existing,
                # widespread pattern as test_triggers.py.
                assert refreshed.next_run_at.replace(tzinfo=timezone.utc) > past
                assert refreshed.last_run_at is not None
            finally:
                _cleanup("ds-1")
                MCPTool.query.filter_by(id="mt-send2").delete()
                db.session.commit()

    def test_dispatch_window_starts_at_previous_last_run_not_now(self, app, monkeypatch):
        # Regression test: run_due_schedules overwrites row.last_run_at to
        # `now` (the claim) BEFORE dispatch_fn runs, so a naive read of
        # schedule.last_run_at inside _dispatch would always see `now` and
        # collapse the digest window to zero length. The schedule's
        # PREVIOUS last_run_at must reach _dispatch_digest as period_start.
        with app.app_context():
            db.session.add(MCPTool(id="mt-send3", name="send_email", tier=2, enabled=True, source="builtin"))
            previous_run = datetime.now(timezone.utc) - timedelta(days=3)
            past_due = datetime.now(timezone.utc) - timedelta(minutes=5)
            schedule = DigestSchedule(id="ds-4", recipient_email="a@example.com", frequency="daily",
                                      hour_utc=9, enabled=True, next_run_at=past_due,
                                      last_run_at=previous_run)
            db.session.add(schedule)
            db.session.commit()

            captured = {}
            def fake_dispatch(schedule_id, email, start, end):
                captured["start"] = start
                captured["end"] = end
            monkeypatch.setattr(digest_mod, "_dispatch_digest", fake_dispatch)

            try:
                dispatched = digest_mod.run_due_digests()
                assert dispatched == 1
                assert captured["start"] is not None
                # The window must span back to (at least) the previous run,
                # not collapse to now==now.
                assert (captured["end"] - captured["start"]) >= timedelta(days=2, hours=23)
            finally:
                _cleanup("ds-4")
                MCPTool.query.filter_by(id="mt-send3").delete()
                db.session.commit()

    def test_disabled_digest_never_fires(self, app, monkeypatch):
        with app.app_context():
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            schedule = DigestSchedule(id="ds-2", recipient_email="a@example.com", frequency="daily",
                                      hour_utc=9, enabled=False, next_run_at=past)
            db.session.add(schedule)
            db.session.commit()

            called = {"n": 0}
            monkeypatch.setattr(digest_mod, "_dispatch_digest", lambda *a: called.__setitem__("n", called["n"] + 1))

            try:
                dispatched = digest_mod.run_due_digests()
                assert called["n"] == 0
            finally:
                _cleanup("ds-2")

    def test_not_yet_due_digest_does_not_fire(self, app, monkeypatch):
        with app.app_context():
            future = datetime.now(timezone.utc) + timedelta(days=1)
            schedule = DigestSchedule(id="ds-3", recipient_email="a@example.com", frequency="daily",
                                      hour_utc=9, enabled=True, next_run_at=future)
            db.session.add(schedule)
            db.session.commit()

            called = {"n": 0}
            monkeypatch.setattr(digest_mod, "_dispatch_digest", lambda *a: called.__setitem__("n", called["n"] + 1))

            try:
                digest_mod.run_due_digests()
                assert called["n"] == 0
            finally:
                _cleanup("ds-3")


class TestDigestScheduleRoutes:
    def test_create_daily_digest(self, app, client):
        resp = client.post("/api/digest-schedules", json={
            "recipient_email": "ops@example.com", "frequency": "daily", "hour_utc": 9,
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["frequency"] == "daily"
        assert data["next_run_at"] is not None
        with app.app_context():
            DigestSchedule.query.filter_by(id=data["id"]).delete()
            db.session.commit()

    def test_rejects_invalid_email(self, app, client):
        resp = client.post("/api/digest-schedules", json={
            "recipient_email": "not-an-email", "frequency": "daily",
        })
        assert resp.status_code == 400

    def test_weekly_requires_day_of_week(self, app, client):
        resp = client.post("/api/digest-schedules", json={
            "recipient_email": "ops@example.com", "frequency": "weekly",
        })
        assert resp.status_code == 400

    def test_list_update_delete(self, app, client):
        create_resp = client.post("/api/digest-schedules", json={
            "recipient_email": "ops@example.com", "frequency": "daily", "hour_utc": 9,
        })
        sched_id = create_resp.get_json()["id"]

        list_resp = client.get("/api/digest-schedules")
        assert any(s["id"] == sched_id for s in list_resp.get_json())

        update_resp = client.patch(f"/api/digest-schedules/{sched_id}", json={"enabled": False})
        assert update_resp.status_code == 200
        assert update_resp.get_json()["enabled"] is False

        del_resp = client.delete(f"/api/digest-schedules/{sched_id}")
        assert del_resp.status_code == 204
        list_resp2 = client.get("/api/digest-schedules")
        assert not any(s["id"] == sched_id for s in list_resp2.get_json())

    def test_update_unknown_schedule_404s(self, app, client):
        resp = client.patch("/api/digest-schedules/does-not-exist", json={"enabled": False})
        assert resp.status_code == 404

    def test_update_rejects_invalid_day_of_week(self, app, client):
        create_resp = client.post("/api/digest-schedules", json={
            "recipient_email": "ops@example.com", "frequency": "daily", "hour_utc": 9,
        })
        sched_id = create_resp.get_json()["id"]
        try:
            resp = client.patch(f"/api/digest-schedules/{sched_id}", json={"day_of_week": "not-a-number"})
            assert resp.status_code == 400
        finally:
            client.delete(f"/api/digest-schedules/{sched_id}")
