"""
Tests for ChatRoom's linked-work-item fields (linked_epic_id/linked_feature_id/
linked_task_id), added so "Plan with AI" can create a room instead of a 1:1
session and still track which work item it's planning.
"""
from app import db
from app.models.chat_room import ChatRoom


class TestChatRoomLinkedFields:
    def test_to_dict_includes_linked_fields(self, app):
        with app.app_context():
            room = ChatRoom(id="r-link", name="r-link", linked_task_id="t-1")
            db.session.add(room)
            db.session.commit()
            try:
                d = room.to_dict()
                assert d["linked_task_id"] == "t-1"
                assert d["linked_epic_id"] is None
                assert d["linked_feature_id"] is None
            finally:
                ChatRoom.query.filter_by(id="r-link").delete()
                db.session.commit()


class TestCreateRoomWithLinkedItem:
    def test_create_room_persists_linked_task_id(self, app, client):
        resp = client.post("/api/chat-rooms", json={
            "name": "● TASK · Ship the widget",
            "room_type": "planning",
            "linked_task_id": "t-42",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["linked_task_id"] == "t-42"
        with app.app_context():
            ChatRoom.query.filter_by(id=data["id"]).delete()
            db.session.commit()

    def test_create_room_missing_agent_id_is_non_fatal(self, app, client):
        # A stale/renamed agent id in agent_ids must not block room creation.
        resp = client.post("/api/chat-rooms", json={
            "name": "Planning room",
            "agent_ids": ["nonexistent-agent-id"],
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["member_count"] == 0
        with app.app_context():
            ChatRoom.query.filter_by(id=data["id"]).delete()
            db.session.commit()
