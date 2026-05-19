"""Chat Room Goal — tracks the current goal/objective for a chat room session."""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class ChatRoomGoal(db.Model):
    __tablename__ = "chat_room_goals"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    room_id = db.Column(db.String(36), db.ForeignKey("chat_rooms.id"), nullable=False, index=True)

    goal_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), default="active")  # active | completed | abandoned
    set_by = db.Column(db.String(64), default="user")    # user | agent

    created_at = db.Column(db.DateTime, default=_now)
    completed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "room_id": self.room_id,
            "goal_text": self.goal_text,
            "status": self.status,
            "set_by": self.set_by,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
