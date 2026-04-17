"""
Chat sessions and messages.
A Session is a named conversation thread. Messages belong to a session.
Sessions can optionally be linked to a Task/Feature/Epic.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    name = db.Column(db.String(256), nullable=False, default="New conversation")
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # Optional link to work hierarchy
    linked_epic_id = db.Column(db.String(36), db.ForeignKey("epics.id"), nullable=True)
    linked_feature_id = db.Column(db.String(36), db.ForeignKey("features.id"), nullable=True)
    linked_task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=True)

    # Context management
    context_strategy = db.Column(db.String(32), default="full")  # full|sliding|summarize
    total_tokens_used = db.Column(db.Integer, default=0)
    is_agent_session = db.Column(db.Boolean, default=False)  # agent run sessions

    messages = db.relationship("Message", back_populates="session",
                                cascade="all, delete-orphan", order_by="Message.created_at")
    compactions = db.relationship("ContextCompaction", back_populates="session",
                                   cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "context_strategy": self.context_strategy,
            "total_tokens_used": self.total_tokens_used,
            "is_agent_session": self.is_agent_session,
            "linked_task_id": self.linked_task_id,
            "linked_feature_id": self.linked_feature_id,
            "linked_epic_id": self.linked_epic_id,
            "message_count": len(self.messages),
        }


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    session_id = db.Column(db.String(36), db.ForeignKey("sessions.id"), nullable=False)
    role = db.Column(db.String(16), nullable=False)   # user|assistant|system|tool
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_now)

    # Token tracking
    token_count = db.Column(db.Integer, default=0)

    # PII tracking
    pii_detected = db.Column(db.Boolean, default=False)
    pii_types = db.Column(db.String(256), nullable=True)  # comma-separated types found

    # Pinned messages always stay in context window
    pinned = db.Column(db.Boolean, default=False)

    # Tool call metadata (for role=tool or assistant tool_use)
    tool_call_id = db.Column(db.String(128), nullable=True)
    tool_name = db.Column(db.String(128), nullable=True)

    session = db.relationship("Session", back_populates="messages")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "token_count": self.token_count,
            "pii_detected": self.pii_detected,
            "pii_types": self.pii_types,
            "pinned": self.pinned,
            "tool_name": self.tool_name,
        }


class ContextCompaction(db.Model):
    """Record of a context compaction event — summary replaces old messages."""
    __tablename__ = "context_compactions"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    session_id = db.Column(db.String(36), db.ForeignKey("sessions.id"), nullable=False)
    compacted_at = db.Column(db.DateTime, default=_now)

    messages_compacted = db.Column(db.Integer, default=0)
    tokens_before = db.Column(db.Integer, default=0)
    tokens_after = db.Column(db.Integer, default=0)
    summary = db.Column(db.Text, nullable=False)

    # Original messages archived as JSON (recoverable)
    archived_messages = db.Column(db.Text, nullable=True)  # JSON array

    session = db.relationship("Session", back_populates="compactions")
