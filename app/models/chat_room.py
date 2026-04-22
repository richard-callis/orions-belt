"""
Chat Rooms — multi-participant group chat spaces for agents and the user.
Distinct from one-on-one AI Sessions: rooms persist, have members, and support
human + agent messages in a shared thread.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


ROOM_TYPES = ("general", "task", "planning", "ops")


class ChatRoom(db.Model):
    __tablename__ = "chat_rooms"

    id          = db.Column(db.String(36),  primary_key=True, default=_uuid)
    name        = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text,        nullable=True)
    room_type   = db.Column(db.String(32),  default="general")  # general|task|planning|ops

    # Optional link to a task
    task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    members  = db.relationship("ChatRoomMember",  back_populates="room",
                                cascade="all, delete-orphan")
    messages = db.relationship("ChatRoomMessage", back_populates="room",
                                cascade="all, delete-orphan",
                                order_by="ChatRoomMessage.created_at")

    def to_dict(self, include_messages=False):
        d = {
            "id":           self.id,
            "name":         self.name,
            "description":  self.description,
            "room_type":    self.room_type,
            "task_id":      self.task_id,
            "created_at":   self.created_at.isoformat(),
            "updated_at":   self.updated_at.isoformat(),
            "member_count": len(self.members),
            "message_count": len(self.messages),
            "members": [m.to_dict() for m in self.members],
        }
        if include_messages:
            d["messages"] = [m.to_dict() for m in self.messages[-100:]]
        return d


class ChatRoomMember(db.Model):
    __tablename__ = "chat_room_members"

    id       = db.Column(db.String(36), primary_key=True, default=_uuid)
    room_id  = db.Column(db.String(36), db.ForeignKey("chat_rooms.id"), nullable=False)
    agent_id = db.Column(db.String(36), db.ForeignKey("agents.id"),     nullable=True)
    # null agent_id = the human user

    role       = db.Column(db.String(32), default="member")  # lead|member|readonly
    joined_at  = db.Column(db.DateTime,   default=_now)
    last_read_at = db.Column(db.DateTime, nullable=True)

    room  = db.relationship("ChatRoom", back_populates="members")
    agent = db.relationship("Agent")

    def to_dict(self):
        return {
            "id":       self.id,
            "room_id":  self.room_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent.name if self.agent else None,
            "role":     self.role,
            "joined_at": self.joined_at.isoformat(),
        }


class ChatRoomMessage(db.Model):
    __tablename__ = "chat_room_messages"

    id          = db.Column(db.String(36), primary_key=True, default=_uuid)
    room_id     = db.Column(db.String(36), db.ForeignKey("chat_rooms.id"), nullable=False)
    agent_id    = db.Column(db.String(36), db.ForeignKey("agents.id"),     nullable=True)
    # null agent_id = human (or system)

    sender_type = db.Column(db.String(16), default="human")  # human|agent|system
    content     = db.Column(db.Text, nullable=False)
    created_at  = db.Column(db.DateTime, default=_now, index=True)

    room  = db.relationship("ChatRoom", back_populates="messages")
    agent = db.relationship("Agent")

    def to_dict(self):
        return {
            "id":          self.id,
            "room_id":     self.room_id,
            "agent_id":    self.agent_id,
            "agent_name":  self.agent.name if self.agent else None,
            "sender_type": self.sender_type,
            "content":     self.content,
            "created_at":  self.created_at.isoformat(),
        }
