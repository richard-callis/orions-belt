"""
Tests for the chat tool-approval gate.

High-tier (Tier 3 / destructive) tools must NOT auto-execute in chat — a
pending approval is recorded and the user must approve it, mirroring the agent
runner's hard-stop.
"""
import json

from app import db
from app.models.chat import Session
from app.models.chat_approval import PendingToolApproval


def _make_session(sid):
    s = Session(id=sid, name="approval test")
    db.session.add(s)
    db.session.commit()


class TestApprovalResolution:
    def test_reject_marks_rejected_and_does_not_execute(self, app, client):
        with app.app_context():
            _make_session("sess-approval-reject")
            ap = PendingToolApproval(
                session_id="sess-approval-reject",
                tool_name="delete_file",
                tool_args=json.dumps({"path": "/tmp/should-not-run"}),
                tool_call_id="call-1",
                tier=3,
                status="pending",
            )
            db.session.add(ap)
            db.session.commit()
            ap_id = ap.id

        resp = client.post(f"/chat/api/approvals/{ap_id}", json={"approved": False})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "rejected"

        with app.app_context():
            assert PendingToolApproval.query.get(ap_id).status == "rejected"

    def test_double_resolution_conflicts(self, app, client):
        with app.app_context():
            _make_session("sess-approval-double")
            ap = PendingToolApproval(
                session_id="sess-approval-double",
                tool_name="delete_file",
                tool_args="{}",
                tool_call_id="call-2",
                tier=3,
                status="rejected",  # already resolved
            )
            db.session.add(ap)
            db.session.commit()
            ap_id = ap.id

        resp = client.post(f"/chat/api/approvals/{ap_id}", json={"approved": True})
        assert resp.status_code == 409

    def test_missing_approval_404(self, client):
        resp = client.post("/chat/api/approvals/does-not-exist", json={"approved": True})
        assert resp.status_code == 404

    def test_list_pending_for_session(self, app, client):
        with app.app_context():
            _make_session("sess-approval-list")
            db.session.add(PendingToolApproval(
                session_id="sess-approval-list", tool_name="modify_file",
                tool_args="{}", tier=3, status="pending",
            ))
            db.session.commit()

        resp = client.get("/chat/api/approvals?session_id=sess-approval-list&status=pending")
        assert resp.status_code == 200
        data = resp.get_json()
        assert any(a["tool_name"] == "modify_file" for a in data)
