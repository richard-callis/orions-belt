"""
Tests for room agent replies: mention parsing, history building, and the
post_message trigger.
"""
from types import SimpleNamespace

from app import db
from app.models.chat_room import ChatRoom, ChatRoomMember, ChatRoomMessage
from app.models.agent import Agent
from app.routes.chat_rooms import (
    _normalize_handle,
    _mentioned_agents,
    _build_room_history,
)


def _agent(name):
    return SimpleNamespace(id=name, name=name, system_prompt=None, llm_model_override=None)


class TestMentionParsing:
    def test_normalize_handle(self):
        assert _normalize_handle("Data Analyst") == "data-analyst"
        assert _normalize_handle("Nova") == "nova"

    def test_mention_by_first_name(self):
        agents = [_agent("Nova"), _agent("Atlas")]
        hits = _mentioned_agents("hey @nova can you help", agents)
        assert [a.name for a in hits] == ["Nova"]

    def test_mention_by_full_handle(self):
        agents = [_agent("Data Analyst"), _agent("Nova")]
        hits = _mentioned_agents("@data-analyst please review", agents)
        assert [a.name for a in hits] == ["Data Analyst"]

    def test_no_mention_returns_empty(self):
        agents = [_agent("Nova")]
        assert _mentioned_agents("just a normal message", agents) == []


class TestBuildRoomHistory:
    def test_maps_roles_and_labels_other_agents(self, app):
        with app.app_context():
            a1 = Agent(id="a1", name="Nova", system_prompt="You are Nova.", status="idle")
            a2 = Agent(id="a2", name="Atlas", status="idle")
            room = ChatRoom(id="r1", name="room")
            db.session.add_all([a1, a2, room])
            db.session.commit()
            db.session.add_all([
                ChatRoomMessage(id="m1", room_id="r1", sender_type="human", content="hello"),
                ChatRoomMessage(id="m2", room_id="r1", sender_type="agent", agent_id="a1", content="hi from nova"),
                ChatRoomMessage(id="m3", room_id="r1", sender_type="agent", agent_id="a2", content="atlas here"),
                ChatRoomMessage(id="m4", room_id="r1", sender_type="system", content="x joined"),
            ])
            db.session.commit()
            try:
                roster = {"a1": "Nova", "a2": "Atlas"}
                msgs = _build_room_history("r1", a1, roster)
                assert msgs[0]["role"] == "system"
                assert "Nova" in msgs[0]["content"]
                # human → user
                assert {"role": "user", "content": "hello"} in msgs
                # own message → assistant (unlabeled)
                assert {"role": "assistant", "content": "hi from nova"} in msgs
                # other agent → user, labeled
                assert any(m["role"] == "user" and m["content"] == "[Atlas]: atlas here" for m in msgs)
                # system messages excluded
                assert all("x joined" not in m["content"] for m in msgs)
            finally:
                ChatRoomMessage.query.filter_by(room_id="r1").delete()
                ChatRoom.query.filter_by(id="r1").delete()
                Agent.query.filter(Agent.id.in_(["a1", "a2"])).delete(synchronize_session=False)
                db.session.commit()


class TestConversationOrchestration:
    """The bounded agent-to-agent burst: cascade via @mention, capped total."""

    def _make_room(self, rid, agent_specs):
        room = ChatRoom(id=rid, name=rid)
        db.session.add(room)
        for aid, name in agent_specs:
            db.session.add(Agent(id=aid, name=name, status="idle"))
            db.session.add(ChatRoomMember(id=f"{rid}-{aid}", room_id=rid, agent_id=aid))
        db.session.commit()

    def _cleanup(self, rid, agent_ids):
        ChatRoomMessage.query.filter_by(room_id=rid).delete()
        ChatRoomMember.query.filter_by(room_id=rid).delete()
        ChatRoom.query.filter_by(id=rid).delete()
        Agent.query.filter(Agent.id.in_(agent_ids)).delete(synchronize_session=False)
        db.session.commit()

    def test_cap_prevents_runaway(self, app, monkeypatch):
        import app.routes.chat_rooms as cr
        monkeypatch.setattr("app.routes.settings._get_active_provider",
                            lambda: {"base_url": "x", "api_key": "y", "model": "m"})
        # Each agent always @mentions the other → infinite cascade without the cap.
        def fake_reply(agent, *a, **k):
            other = "atlas" if agent.name.lower() == "nova" else "nova"
            return f"passing to @{other}"
        monkeypatch.setattr(cr, "_generate_agent_reply", fake_reply)
        with app.app_context():
            self._make_room("r-cap", [("nova", "Nova"), ("atlas", "Atlas")])
            try:
                cr._run_room_conversation(app, "r-cap", "everyone talk")
                count = ChatRoomMessage.query.filter_by(
                    room_id="r-cap", sender_type="agent").count()
                assert count == cr._max_agent_turns()   # capped, not infinite
            finally:
                self._cleanup("r-cap", ["nova", "atlas"])

    def test_admin_setting_overrides_cap(self, app, monkeypatch):
        import app.routes.chat_rooms as cr
        from app.models.settings import Setting
        monkeypatch.setattr("app.routes.settings._get_active_provider",
                            lambda: {"base_url": "x", "api_key": "y", "model": "m"})
        def fake_reply(agent, *a, **k):
            other = "atlas" if agent.name.lower() == "nova" else "nova"
            return f"over to @{other}"
        monkeypatch.setattr(cr, "_generate_agent_reply", fake_reply)
        with app.app_context():
            Setting.set("agents.max_agent_turns", "3", value_type="string")
            db.session.commit()
            self._make_room("r-set", [("nova", "Nova"), ("atlas", "Atlas")])
            try:
                assert cr._max_agent_turns() == 3
                cr._run_room_conversation(app, "r-set", "everyone talk")
                count = ChatRoomMessage.query.filter_by(
                    room_id="r-set", sender_type="agent").count()
                assert count == 3   # honored the admin setting, not the default 6
            finally:
                Setting.set("agents.max_agent_turns", "", value_type="string")
                db.session.commit()
                self._cleanup("r-set", ["nova", "atlas"])

    def test_no_cascade_when_no_mentions(self, app, monkeypatch):
        import app.routes.chat_rooms as cr
        monkeypatch.setattr("app.routes.settings._get_active_provider",
                            lambda: {"base_url": "x", "api_key": "y", "model": "m"})
        monkeypatch.setattr(cr, "_generate_agent_reply", lambda agent, *a, **k: "just a reply")
        with app.app_context():
            self._make_room("r-nc", [("nova", "Nova"), ("atlas", "Atlas")])
            try:
                cr._run_room_conversation(app, "r-nc", "hi everyone")
                # Each of the 2 agents replies once; no one is pulled back in.
                count = ChatRoomMessage.query.filter_by(
                    room_id="r-nc", sender_type="agent").count()
                assert count == 2
            finally:
                self._cleanup("r-nc", ["nova", "atlas"])

    def test_targeted_mention_only_that_agent(self, app, monkeypatch):
        import app.routes.chat_rooms as cr
        monkeypatch.setattr("app.routes.settings._get_active_provider",
                            lambda: {"base_url": "x", "api_key": "y", "model": "m"})
        monkeypatch.setattr(cr, "_generate_agent_reply", lambda agent, *a, **k: "on it")
        with app.app_context():
            self._make_room("r-tm", [("nova", "Nova"), ("atlas", "Atlas")])
            try:
                cr._run_room_conversation(app, "r-tm", "hey @nova can you look")
                replies = ChatRoomMessage.query.filter_by(
                    room_id="r-tm", sender_type="agent").all()
                assert len(replies) == 1
                assert replies[0].agent_id == "nova"
            finally:
                self._cleanup("r-tm", ["nova", "atlas"])


class TestPostMessagePiiScan:
    """Human room messages are PII-scanned before persisting (so sanitized text
    is what agents see on every subsequent round, not just the first)."""

    def test_human_message_is_sanitized_before_storing(self, app, client, monkeypatch):
        from app.models.chat_room import ChatRoom, ChatRoomMessage

        class FakeGuard:
            def scan(self, text, session_id=None, direction="outbound"):
                return text.replace("secret@example.com", "[PII:EMAIL:abc123]"), True, ["EMAIL"]

        monkeypatch.setattr(
            "app.services.pii_guard.get_pii_guard", lambda: FakeGuard()
        )
        with app.app_context():
            db.session.add(ChatRoom(id="r-pii", name="r-pii"))
            db.session.commit()
        try:
            resp = client.post("/api/chat-rooms/r-pii/messages",
                               json={"content": "email me at secret@example.com"})
            assert resp.status_code == 201
            data = resp.get_json()
            assert "secret@example.com" not in data["content"]
            assert "[PII:EMAIL:abc123]" in data["content"]
            with app.app_context():
                stored = ChatRoomMessage.query.filter_by(room_id="r-pii").first()
                assert "secret@example.com" not in stored.content
        finally:
            with app.app_context():
                ChatRoomMessage.query.filter_by(room_id="r-pii").delete()
                ChatRoom.query.filter_by(id="r-pii").delete()
                db.session.commit()

    def test_scan_failure_does_not_block_posting(self, app, client, monkeypatch):
        from app.models.chat_room import ChatRoom, ChatRoomMessage

        def broken_guard():
            raise RuntimeError("model not loaded")

        monkeypatch.setattr("app.services.pii_guard.get_pii_guard", broken_guard)
        with app.app_context():
            db.session.add(ChatRoom(id="r-pii-fail", name="r-pii-fail"))
            db.session.commit()
        try:
            resp = client.post("/api/chat-rooms/r-pii-fail/messages", json={"content": "hello"})
            assert resp.status_code == 201
            assert resp.get_json()["content"] == "hello"
        finally:
            with app.app_context():
                ChatRoomMessage.query.filter_by(room_id="r-pii-fail").delete()
                ChatRoom.query.filter_by(id="r-pii-fail").delete()
                db.session.commit()


class TestAgentReplyPiiScanAndRestore:
    """Agent replies are scanned before persisting (previously only human
    messages were), and ChatRoomMessage.to_dict() restores tokens for display
    while the stored/re-fed copy stays tokenized."""

    def _fake_guard(self):
        class FakeGuard:
            def scan(self, text, session_id=None, direction="outbound"):
                if "secret@example.com" in text:
                    return text.replace("secret@example.com", "[PII:EMAIL:abc123]"), True, ["EMAIL"]
                return text, False, []

            def restore(self, text):
                return text.replace("[PII:EMAIL:abc123]", "secret@example.com")
        return FakeGuard()

    def test_agent_reply_is_sanitized_before_storing(self, app, monkeypatch):
        import app.routes.chat_rooms as cr
        monkeypatch.setattr("app.routes.settings._get_active_provider",
                            lambda: {"base_url": "x", "api_key": "y", "model": "m"})
        monkeypatch.setattr(cr, "_generate_agent_reply",
                            lambda agent, *a, **k: "Contact me at secret@example.com")
        monkeypatch.setattr("app.services.pii_guard.get_pii_guard", lambda: self._fake_guard())
        with app.app_context():
            db.session.add(ChatRoom(id="r-agent-pii", name="r-agent-pii"))
            db.session.add(Agent(id="nova", name="Nova", status="idle"))
            db.session.add(ChatRoomMember(id="m1", room_id="r-agent-pii", agent_id="nova"))
            db.session.commit()
            try:
                cr._run_room_conversation(app, "r-agent-pii", "hi")
                msg = ChatRoomMessage.query.filter_by(room_id="r-agent-pii", sender_type="agent").first()
                # Stored copy is tokenized — never re-feed plaintext PII into context.
                assert "secret@example.com" not in msg.content
                assert "[PII:EMAIL:abc123]" in msg.content
                # Read path (to_dict) restores it for display.
                assert msg.to_dict()["content"] == "Contact me at secret@example.com"
            finally:
                ChatRoomMessage.query.filter_by(room_id="r-agent-pii").delete()
                ChatRoomMember.query.filter_by(room_id="r-agent-pii").delete()
                ChatRoom.query.filter_by(id="r-agent-pii").delete()
                Agent.query.filter_by(id="nova").delete()
                db.session.commit()

    def test_human_message_restored_on_read_but_tokenized_in_history(self, app, monkeypatch):
        import app.routes.chat_rooms as cr
        monkeypatch.setattr("app.services.pii_guard.get_pii_guard", lambda: self._fake_guard())
        with app.app_context():
            room, agent = ChatRoom(id="r-restore", name="r-restore"), Agent(id="a-restore", name="A", status="idle")
            db.session.add_all([room, agent])
            db.session.commit()
            msg = ChatRoomMessage(
                id="msg-restore", room_id="r-restore", sender_type="human",
                content=cr._sanitize_for_room("email secret@example.com", "r-restore"),
            )
            db.session.add(msg)
            db.session.commit()
            try:
                # Stored content is tokenized (what gets fed back as agent context).
                assert "secret@example.com" not in msg.content
                # to_dict() restores it for display.
                assert msg.to_dict()["content"] == "email secret@example.com"
            finally:
                ChatRoomMessage.query.filter_by(room_id="r-restore").delete()
                ChatRoom.query.filter_by(id="r-restore").delete()
                Agent.query.filter_by(id="a-restore").delete()
                db.session.commit()


class TestPostMessageTrigger:
    def test_human_post_returns_message(self, app, client):
        with app.app_context():
            room = ChatRoom(id="r-post", name="room")
            db.session.add(room)
            db.session.commit()
        try:
            resp = client.post("/api/chat-rooms/r-post/messages", json={"content": "hi"})
            assert resp.status_code == 201
            assert resp.get_json()["sender_type"] == "human"
        finally:
            with app.app_context():
                ChatRoomMessage.query.filter_by(room_id="r-post").delete()
                ChatRoom.query.filter_by(id="r-post").delete()
                db.session.commit()

    def test_missing_room_404(self, client):
        resp = client.post("/api/chat-rooms/does-not-exist/messages", json={"content": "hi"})
        assert resp.status_code == 404
