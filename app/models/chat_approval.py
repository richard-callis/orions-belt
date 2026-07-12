"""
Pending tool-approval records for the chat tool loop.

When the model requests a high-tier (destructive) tool during a chat turn, the
call is NOT executed automatically. A PendingToolApproval row is written and the
user must explicitly approve it before it runs — mirroring the agent runner's
Tier-3 hard-stop, so untrusted content can't drive an unattended delete/modify.
"""
import uuid
from datetime import datetime, timezone

from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class PendingToolApproval(db.Model):
    __tablename__ = "pending_tool_approvals"

    id            = db.Column(db.String(36), primary_key=True, default=_uuid)
    session_id    = db.Column(db.String(36), db.ForeignKey("sessions.id"), nullable=False, index=True)
    run_id        = db.Column(db.String(36), nullable=True)

    tool_name     = db.Column(db.String(128), nullable=False)
    tool_args     = db.Column(db.Text, nullable=False, default="{}")  # JSON
    tool_call_id  = db.Column(db.String(128), nullable=True)
    tier          = db.Column(db.Integer, nullable=False, default=3)

    # pending | approved | rejected | executed | failed
    status        = db.Column(db.String(16), nullable=False, default="pending")
    result        = db.Column(db.Text, nullable=True)

    created_at    = db.Column(db.DateTime, default=_now)
    resolved_at   = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        import json
        try:
            args = json.loads(self.tool_args) if self.tool_args else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        return {
            "id": self.id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "tool_name": self.tool_name,
            "tool_args": args,
            "tool_call_id": self.tool_call_id,
            "tier": self.tier,
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
