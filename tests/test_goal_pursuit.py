"""
Tests for goal-driven autonomous agent pursuit in chat rooms.
"""
from app import db
from app.models.chat_room import ChatRoom, ChatRoomMember, ChatRoomMessage
from app.models.chat_room_goal import ChatRoomGoal
from app.models.agent import Agent
from app.models.settings import Setting
import app.routes.chat_rooms as cr


def _make_room_with_agent(rid, aid="lead-agent", name="Lead"):
    room = ChatRoom(id=rid, name=rid)
    agent = Agent(id=aid, name=name, status="idle")
    db.session.add_all([room, agent])
    db.session.add(ChatRoomMember(id=f"{rid}-{aid}", room_id=rid, agent_id=aid, role="lead"))
    db.session.commit()
    return room, agent


def _cleanup(rid, agent_ids):
    ChatRoomGoal.query.filter_by(room_id=rid).delete()
    ChatRoomMessage.query.filter_by(room_id=rid).delete()
    ChatRoomMember.query.filter_by(room_id=rid).delete()
    ChatRoom.query.filter_by(id=rid).delete()
    Agent.query.filter(Agent.id.in_(agent_ids)).delete(synchronize_session=False)
    db.session.commit()


class TestMaxGoalRounds:
    def test_default(self, app):
        with app.app_context():
            assert cr._max_goal_rounds() == cr._MAX_GOAL_ROUNDS_DEFAULT

    def test_admin_setting_clamped_to_ceiling(self, app):
        with app.app_context():
            Setting.set("agents.max_goal_rounds", "9999", value_type="string")
            db.session.commit()
            try:
                assert cr._max_goal_rounds() == cr._MAX_GOAL_ROUNDS_CEILING
            finally:
                Setting.set("agents.max_goal_rounds", "", value_type="string")
                db.session.commit()


class TestGoalLeadAgent:
    def test_prefers_lead_role(self, app):
        with app.app_context():
            room, lead = _make_room_with_agent("r-lead", "a-lead", "Lead")
            member = Agent(id="a-member", name="Member", status="idle")
            db.session.add(member)
            db.session.add(ChatRoomMember(id="r-lead-a-member", room_id="r-lead", agent_id="a-member", role="member"))
            db.session.commit()
            try:
                chosen = cr._goal_lead_agent("r-lead", [lead, member])
                assert chosen.id == "a-lead"
            finally:
                _cleanup("r-lead", ["a-lead", "a-member"])

    def test_falls_back_to_first_agent_when_no_lead(self, app):
        with app.app_context():
            room = ChatRoom(id="r-nolead", name="r-nolead")
            a1 = Agent(id="a1", name="A1", status="idle")
            a2 = Agent(id="a2", name="A2", status="idle")
            db.session.add_all([room, a1, a2])
            db.session.add(ChatRoomMember(id="m1", room_id="r-nolead", agent_id="a1", role="member"))
            db.session.add(ChatRoomMember(id="m2", room_id="r-nolead", agent_id="a2", role="member"))
            db.session.commit()
            try:
                chosen = cr._goal_lead_agent("r-nolead", [a1, a2])
                assert chosen.id == "a1"
            finally:
                _cleanup("r-nolead", ["a1", "a2"])

    def test_no_agents_returns_none(self, app):
        with app.app_context():
            assert cr._goal_lead_agent("nonexistent-room", []) is None


class TestBuildGoalHistory:
    def test_injects_goal_and_markers(self, app):
        with app.app_context():
            room, agent = _make_room_with_agent("r-hist", "a-hist")
            try:
                msgs = cr._build_goal_history("r-hist", agent, {"a-hist": "Lead"}, "Ship the widget")
                sys = msgs[0]["content"]
                assert "Ship the widget" in sys
                assert cr._GOAL_COMPLETE_MARKER in sys
                assert cr._HELP_NEEDED_MARKER in sys
            finally:
                _cleanup("r-hist", ["a-hist"])


class TestGoalSignalParsing:
    def test_complete_marker_on_last_line(self):
        shown, signal, q = cr._parse_goal_signal(f"I finished the work.\n{cr._GOAL_COMPLETE_MARKER}")
        assert signal == "complete"
        assert cr._GOAL_COMPLETE_MARKER not in shown

    def test_help_marker_on_last_line(self):
        shown, signal, q = cr._parse_goal_signal(f"Progress so far.\n{cr._HELP_NEEDED_MARKER}: which env?")
        assert signal == "help"
        assert q == "which env?"
        assert cr._HELP_NEEDED_MARKER not in shown

    def test_marker_mentioned_in_prose_is_not_a_signal(self):
        reply = f"I'll say {cr._GOAL_COMPLETE_MARKER} when the work is actually done. Still working."
        shown, signal, q = cr._parse_goal_signal(reply)
        assert signal is None
        assert shown == reply  # unchanged — not treated as a control signal

    def test_marker_not_on_last_line_is_not_a_signal(self):
        reply = f"{cr._GOAL_COMPLETE_MARKER}\nActually wait, one more thing to check."
        shown, signal, q = cr._parse_goal_signal(reply)
        assert signal is None

    def test_plain_reply_no_signal(self):
        shown, signal, q = cr._parse_goal_signal("Just an update, still working.")
        assert signal is None
        assert shown == "Just an update, still working."


class TestRunGoalPursuit:
    def _setup(self, app, monkeypatch, rid="r-goal"):
        monkeypatch.setattr(
            "app.services.agents.runtime.resolve_active_provider",
            lambda: {"base_url": "x", "api_key": "y", "model": "m"},
        )
        with app.app_context():
            room, agent = _make_room_with_agent(rid)
            goal = ChatRoomGoal(room_id=rid, goal_text="Do the thing", status="active")
            db.session.add(goal)
            db.session.commit()
            gen = cr._bump_goal_generation(goal.id)
            return goal.id, gen

    def test_completes_when_reviewer_approves(self, app, monkeypatch):
        from app.services.agents.runtime import AgentRuntime
        monkeypatch.setattr(AgentRuntime, "chat_reply", lambda self, *a, **k: f"All done.\n{cr._GOAL_COMPLETE_MARKER}")
        monkeypatch.setattr(cr, "_judge_goal_completion", lambda agent, prov, goal, reply: (True, ""))
        goal_id, gen = self._setup(app, monkeypatch, "r-complete")
        try:
            with app.app_context():
                cr._run_goal_pursuit(app, "r-complete", goal_id, gen)
                goal = ChatRoomGoal.query.get(goal_id)
                assert goal.status == "completed"
                assert goal.completed_at is not None
                agent_msgs = ChatRoomMessage.query.filter_by(room_id="r-complete", sender_type="agent").all()
                assert len(agent_msgs) == 1
                assert cr._GOAL_COMPLETE_MARKER not in agent_msgs[0].content  # marker stripped from display
        finally:
            with app.app_context():
                _cleanup("r-complete", ["lead-agent"])

    def test_reviewer_rejection_keeps_looping_with_feedback(self, app, monkeypatch):
        from app.services.agents.runtime import AgentRuntime
        call_count = {"n": 0}
        feedback_seen = {"value": None}

        def fake_reply(self, messages, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return f"Think I'm done.\n{cr._GOAL_COMPLETE_MARKER}"
            # Second round should see the reviewer's rejection reason injected.
            feedback_seen["value"] = messages[0]["content"]
            return "still working, no signal this round"

        monkeypatch.setattr(AgentRuntime, "chat_reply", fake_reply)
        # Cap at 2 rounds so the test is deterministic regardless of what a
        # 3rd round (which would need more mock plumbing) might do.
        monkeypatch.setattr(cr, "_max_goal_rounds", lambda: 2)
        judge_calls = {"n": 0}

        def fake_judge(agent, prov, goal, reply):
            judge_calls["n"] += 1
            return (False, "missing the export step")

        monkeypatch.setattr(cr, "_judge_goal_completion", fake_judge)
        goal_id, gen = self._setup(app, monkeypatch, "r-reject")
        try:
            with app.app_context():
                cr._run_goal_pursuit(app, "r-reject", goal_id, gen)
                assert judge_calls["n"] == 1  # only called when a completion was claimed
                goal = ChatRoomGoal.query.get(goal_id)
                assert goal.status == "active"  # never marked complete
                assert call_count["n"] == 2  # looped after rejection
                assert "missing the export step" in feedback_seen["value"]
        finally:
            with app.app_context():
                _cleanup("r-reject", ["lead-agent"])

    def test_stops_on_help_needed_without_completing(self, app, monkeypatch):
        from app.services.agents.runtime import AgentRuntime
        monkeypatch.setattr(
            AgentRuntime, "chat_reply",
            lambda self, *a, **k: f"{cr._HELP_NEEDED_MARKER}: which environment?"
        )
        goal_id, gen = self._setup(app, monkeypatch, "r-blocked")
        try:
            with app.app_context():
                cr._run_goal_pursuit(app, "r-blocked", goal_id, gen)
                goal = ChatRoomGoal.query.get(goal_id)
                assert goal.status == "active"  # left active, not completed/abandoned
                agent_msgs = ChatRoomMessage.query.filter_by(room_id="r-blocked", sender_type="agent").all()
                assert len(agent_msgs) == 1  # only one round ran before stopping
        finally:
            with app.app_context():
                _cleanup("r-blocked", ["lead-agent"])

    def test_stops_when_goal_cancelled_mid_pursuit(self, app, monkeypatch):
        from app.services.agents.runtime import AgentRuntime
        call_count = {"n": 0}

        def fake_reply(self, *a, **k):
            call_count["n"] += 1
            if call_count["n"] == 2:
                # Simulate the user abandoning the goal between rounds.
                g = ChatRoomGoal.query.get(goal_id)
                g.status = "abandoned"
                db.session.commit()
            return "still working on it"

        monkeypatch.setattr(AgentRuntime, "chat_reply", fake_reply)
        goal_id, gen = self._setup(app, monkeypatch, "r-cancel")
        try:
            with app.app_context():
                cr._run_goal_pursuit(app, "r-cancel", goal_id, gen)
                # Round 3 must never have run since round 2 flipped status away from active.
                assert call_count["n"] == 2
        finally:
            with app.app_context():
                _cleanup("r-cancel", ["lead-agent"])

    def test_stale_generation_never_starts(self, app, monkeypatch):
        """A newer trigger for the same goal supersedes an older, not-yet-started thread."""
        from app.services.agents.runtime import AgentRuntime
        monkeypatch.setattr(AgentRuntime, "chat_reply", lambda self, *a, **k: "should never run")
        goal_id, gen = self._setup(app, monkeypatch, "r-stale")
        try:
            with app.app_context():
                cr._bump_goal_generation(goal_id)  # a newer trigger fires before the old one runs
                cr._run_goal_pursuit(app, "r-stale", goal_id, gen)  # stale generation
                msgs = ChatRoomMessage.query.filter_by(room_id="r-stale").all()
                assert len(msgs) == 0
        finally:
            with app.app_context():
                _cleanup("r-stale", ["lead-agent"])

    def test_round_cap_reached_without_completion(self, app, monkeypatch):
        from app.services.agents.runtime import AgentRuntime
        monkeypatch.setattr(AgentRuntime, "chat_reply", lambda self, *a, **k: "working...")
        monkeypatch.setattr(cr, "_max_goal_rounds", lambda: 3)
        goal_id, gen = self._setup(app, monkeypatch, "r-cap")
        try:
            with app.app_context():
                cr._run_goal_pursuit(app, "r-cap", goal_id, gen)
                goal = ChatRoomGoal.query.get(goal_id)
                assert goal.status == "active"  # not auto-completed
                agent_msgs = ChatRoomMessage.query.filter_by(room_id="r-cap", sender_type="agent").all()
                assert len(agent_msgs) == 3
                system_msgs = ChatRoomMessage.query.filter_by(room_id="r-cap", sender_type="system").all()
                assert any("round limit" in m.content.lower() for m in system_msgs)
        finally:
            with app.app_context():
                _cleanup("r-cap", ["lead-agent"])

    def test_inactive_goal_never_starts(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.services.agents.runtime.resolve_active_provider",
            lambda: {"base_url": "x", "api_key": "y", "model": "m"},
        )
        with app.app_context():
            room, agent = _make_room_with_agent("r-inactive")
            goal = ChatRoomGoal(room_id="r-inactive", goal_text="x", status="completed")
            db.session.add(goal)
            db.session.commit()
            goal_id = goal.id
            gen = cr._bump_goal_generation(goal_id)
        try:
            with app.app_context():
                cr._run_goal_pursuit(app, "r-inactive", goal_id, gen)
                msgs = ChatRoomMessage.query.filter_by(room_id="r-inactive").all()
                assert len(msgs) == 0  # nothing posted — pursuit never started
        finally:
            with app.app_context():
                _cleanup("r-inactive", ["lead-agent"])
