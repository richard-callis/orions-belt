"""
Tests for scheduled room triggers: next-run computation, the dispatch loop
(claim-then-dispatch, room-busy skip, tier ceiling), and the CRUD routes.
"""
from datetime import datetime, timedelta, timezone

from app import db
from app.models.agent import Agent
from app.models.chat_room import ChatRoom, ChatRoomMember, ChatRoomMessage
from app.models.trigger import ScheduledTrigger
import app.services.triggers as triggers_mod


class TestComputeNextRun:
    def test_daily_before_hour_is_today(self):
        after = datetime(2026, 6, 10, 7, 0, tzinfo=timezone.utc)  # Wed 7am
        next_run = triggers_mod._compute_next_run("daily", None, 9, after=after)
        assert next_run == datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)

    def test_daily_after_hour_is_tomorrow(self):
        after = datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc)  # Wed 10am, past 9am
        next_run = triggers_mod._compute_next_run("daily", None, 9, after=after)
        assert next_run == datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)

    def test_weekly_same_day_before_hour(self):
        after = datetime(2026, 6, 8, 7, 0, tzinfo=timezone.utc)  # Monday 7am
        next_run = triggers_mod._compute_next_run("weekly", 0, 9, after=after)  # Monday 9am
        assert next_run == datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)

    def test_weekly_same_day_after_hour_rolls_to_next_week(self):
        after = datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc)  # Monday 10am, past 9am
        next_run = triggers_mod._compute_next_run("weekly", 0, 9, after=after)
        assert next_run == datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)

    def test_weekly_different_day(self):
        after = datetime(2026, 6, 8, 7, 0, tzinfo=timezone.utc)  # Monday
        next_run = triggers_mod._compute_next_run("weekly", 4, 9, after=after)  # Friday
        assert next_run.weekday() == 4
        assert next_run > after

    def test_weekly_no_day_of_week_degrades_to_daily(self):
        after = datetime(2026, 6, 10, 7, 0, tzinfo=timezone.utc)
        next_run = triggers_mod._compute_next_run("weekly", None, 9, after=after)
        assert next_run == datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)

    def test_missed_slots_after_sleep_fire_once_not_repeatedly(self):
        # A machine asleep for 3 days: next_run_at was set days ago, but
        # _compute_next_run is always called with after=now (not the stale
        # next_run_at) — so it advances to the NEXT future slot, not the
        # first missed one.
        long_ago = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        next_run = triggers_mod._compute_next_run("daily", None, 9, after=now)
        assert next_run == datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)
        assert next_run > long_ago


class TestRunDueTriggers:
    def _make_room_with_agent(self, rid, aid):
        room = ChatRoom(id=rid, name=rid)
        agent = Agent(id=aid, name="A", status="idle")
        db.session.add_all([room, agent])
        db.session.add(ChatRoomMember(id=f"{rid}-{aid}", room_id=rid, agent_id=aid))
        db.session.commit()

    def _cleanup(self, rid, aid, trigger_ids=()):
        ScheduledTrigger.query.filter(ScheduledTrigger.id.in_(trigger_ids)).delete(synchronize_session=False)
        ChatRoomMessage.query.filter_by(room_id=rid).delete()
        ChatRoomMember.query.filter_by(room_id=rid).delete()
        ChatRoom.query.filter_by(id=rid).delete()
        Agent.query.filter_by(id=aid).delete()
        db.session.commit()

    def test_due_trigger_advances_next_run_at(self, app, monkeypatch):
        monkeypatch.setattr(triggers_mod, "_dispatch_trigger", lambda *a, **k: None)
        with app.app_context():
            self._make_room_with_agent("r-trig1", "a-trig1")
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            trig = ScheduledTrigger(id="t-trig1", room_id="r-trig1", prompt_text="hi",
                                    frequency="daily", hour_utc=9, enabled=True, next_run_at=past)
            db.session.add(trig)
            db.session.commit()
            try:
                dispatched = triggers_mod.run_due_triggers()
                assert dispatched == 1
                refreshed = ScheduledTrigger.query.get("t-trig1")
                assert refreshed.next_run_at.replace(tzinfo=timezone.utc) > past
                assert refreshed.last_run_at is not None
            finally:
                self._cleanup("r-trig1", "a-trig1", ["t-trig1"])

    def test_disabled_trigger_never_fires(self, app, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(triggers_mod, "_dispatch_trigger", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        with app.app_context():
            self._make_room_with_agent("r-trig2", "a-trig2")
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            trig = ScheduledTrigger(id="t-trig2", room_id="r-trig2", prompt_text="hi",
                                    frequency="daily", hour_utc=9, enabled=False, next_run_at=past)
            db.session.add(trig)
            db.session.commit()
            try:
                triggers_mod.run_due_triggers()
                assert called["n"] == 0
            finally:
                self._cleanup("r-trig2", "a-trig2", ["t-trig2"])

    def test_not_yet_due_trigger_does_not_fire(self, app, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(triggers_mod, "_dispatch_trigger", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        with app.app_context():
            self._make_room_with_agent("r-trig3", "a-trig3")
            future = datetime.now(timezone.utc) + timedelta(hours=1)
            trig = ScheduledTrigger(id="t-trig3", room_id="r-trig3", prompt_text="hi",
                                    frequency="daily", hour_utc=9, enabled=True, next_run_at=future)
            db.session.add(trig)
            db.session.commit()
            try:
                triggers_mod.run_due_triggers()
                assert called["n"] == 0
            finally:
                self._cleanup("r-trig3", "a-trig3", ["t-trig3"])

    def test_busy_room_skips_dispatch_but_still_advances(self, app, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(triggers_mod, "_dispatch_trigger", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        with app.app_context():
            self._make_room_with_agent("r-trig4", "a-trig4")
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            trig = ScheduledTrigger(id="t-trig4", room_id="r-trig4", prompt_text="hi",
                                    frequency="daily", hour_utc=9, enabled=True, next_run_at=past)
            db.session.add(trig)
            db.session.commit()
            import app.routes.chat_rooms as cr
            cr._mark_room_busy("r-trig4")
            try:
                dispatched = triggers_mod.run_due_triggers()
                assert dispatched == 0
                assert called["n"] == 0
                # Claimed anyway — next_run_at still advanced past `past`.
                refreshed = ScheduledTrigger.query.get("t-trig4")
                assert refreshed.next_run_at.replace(tzinfo=timezone.utc) > past
            finally:
                cr._mark_room_free("r-trig4")
                self._cleanup("r-trig4", "a-trig4", ["t-trig4"])

    def test_dispatch_failure_does_not_crash_the_pass(self, app, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(triggers_mod, "_dispatch_trigger", broken)
        with app.app_context():
            self._make_room_with_agent("r-trig5", "a-trig5")
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            trig = ScheduledTrigger(id="t-trig5", room_id="r-trig5", prompt_text="hi",
                                    frequency="daily", hour_utc=9, enabled=True, next_run_at=past)
            db.session.add(trig)
            db.session.commit()
            try:
                dispatched = triggers_mod.run_due_triggers()  # must not raise
                assert dispatched == 0
            finally:
                self._cleanup("r-trig5", "a-trig5", ["t-trig5"])


class TestDispatchTrigger:
    def test_posts_prompt_as_human_message_and_uses_autonomous_tier(self, app, monkeypatch):
        import app.routes.chat_rooms as cr
        captured = {}

        def fake_run_room_conversation(app_, room_id, content, allow_tier=None):
            captured["content"] = content
            captured["allow_tier"] = allow_tier

        monkeypatch.setattr(cr, "_run_room_conversation", fake_run_room_conversation)
        with app.app_context():
            room = ChatRoom(id="r-disp1", name="r-disp1")
            agent = Agent(id="a-disp1", name="A", status="idle")
            db.session.add_all([room, agent])
            db.session.add(ChatRoomMember(id="m-disp1", room_id="r-disp1", agent_id="a-disp1"))
            db.session.commit()
            try:
                triggers_mod._dispatch_trigger(app, "t-disp1", "r-disp1", "write the weekly report")
                assert captured["allow_tier"] == 1
                msg = ChatRoomMessage.query.filter_by(room_id="r-disp1", sender_type="human").first()
                assert msg is not None
                assert "write the weekly report" in msg.content
            finally:
                ChatRoomMessage.query.filter_by(room_id="r-disp1").delete()
                ChatRoomMember.query.filter_by(room_id="r-disp1").delete()
                ChatRoom.query.filter_by(id="r-disp1").delete()
                Agent.query.filter_by(id="a-disp1").delete()
                db.session.commit()

    def test_room_with_no_agents_posts_warning_without_crashing(self, app):
        with app.app_context():
            room = ChatRoom(id="r-disp2", name="r-disp2")
            db.session.add(room)
            db.session.commit()
            try:
                triggers_mod._dispatch_trigger(app, "t-disp2", "r-disp2", "hi")
                warning = ChatRoomMessage.query.filter_by(room_id="r-disp2", sender_type="system").filter(
                    ChatRoomMessage.content.contains("no agents")
                ).first()
                assert warning is not None
            finally:
                ChatRoomMessage.query.filter_by(room_id="r-disp2").delete()
                ChatRoom.query.filter_by(id="r-disp2").delete()
                db.session.commit()


class TestTriggerRoutes:
    def _make_room(self, rid):
        db.session.add(ChatRoom(id=rid, name=rid))
        db.session.commit()

    def test_create_daily_trigger(self, app, client):
        with app.app_context():
            self._make_room("r-route1")
        try:
            resp = client.post("/api/chat-rooms/r-route1/triggers", json={
                "prompt_text": "write the report", "frequency": "daily", "hour_utc": 9,
            })
            assert resp.status_code == 201
            data = resp.get_json()
            assert data["frequency"] == "daily"
            assert data["next_run_at"] is not None
        finally:
            with app.app_context():
                ScheduledTrigger.query.filter_by(room_id="r-route1").delete()
                ChatRoom.query.filter_by(id="r-route1").delete()
                db.session.commit()

    def test_weekly_trigger_requires_day_of_week(self, app, client):
        with app.app_context():
            self._make_room("r-route2")
        try:
            resp = client.post("/api/chat-rooms/r-route2/triggers", json={
                "prompt_text": "x", "frequency": "weekly", "hour_utc": 9,
            })
            assert resp.status_code == 400
        finally:
            with app.app_context():
                ChatRoom.query.filter_by(id="r-route2").delete()
                db.session.commit()

    def test_create_requires_prompt_text(self, app, client):
        with app.app_context():
            self._make_room("r-route3")
        try:
            resp = client.post("/api/chat-rooms/r-route3/triggers", json={"frequency": "daily"})
            assert resp.status_code == 400
        finally:
            with app.app_context():
                ChatRoom.query.filter_by(id="r-route3").delete()
                db.session.commit()

    def test_list_update_delete_trigger(self, app, client):
        with app.app_context():
            self._make_room("r-route4")
        try:
            create_resp = client.post("/api/chat-rooms/r-route4/triggers", json={
                "prompt_text": "original", "frequency": "daily", "hour_utc": 9,
            })
            trig_id = create_resp.get_json()["id"]

            list_resp = client.get("/api/chat-rooms/r-route4/triggers")
            assert any(t["id"] == trig_id for t in list_resp.get_json())

            update_resp = client.patch(f"/api/chat-rooms/triggers/{trig_id}", json={"enabled": False})
            assert update_resp.status_code == 200
            assert update_resp.get_json()["enabled"] is False

            del_resp = client.delete(f"/api/chat-rooms/triggers/{trig_id}")
            assert del_resp.status_code == 204
            list_resp2 = client.get("/api/chat-rooms/r-route4/triggers")
            assert not any(t["id"] == trig_id for t in list_resp2.get_json())
        finally:
            with app.app_context():
                ScheduledTrigger.query.filter_by(room_id="r-route4").delete()
                ChatRoom.query.filter_by(id="r-route4").delete()
                db.session.commit()

    def test_create_on_missing_room_404s(self, app, client):
        resp = client.post("/api/chat-rooms/does-not-exist/triggers", json={
            "prompt_text": "x", "frequency": "daily",
        })
        assert resp.status_code == 404

    def test_update_unknown_trigger_404s(self, app, client):
        resp = client.patch("/api/chat-rooms/triggers/does-not-exist", json={"enabled": False})
        assert resp.status_code == 404
