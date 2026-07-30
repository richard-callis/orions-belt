"""
Scheduled room triggers — post a prompt into a room on a recurring schedule
(e.g. "every Monday 9am UTC: write this week's status report"). See
app/services/triggers.py for the background dispatch loop.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class ScheduledTrigger(db.Model):
    __tablename__ = "scheduled_triggers"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    room_id = db.Column(db.String(36), db.ForeignKey("chat_rooms.id"), nullable=False, index=True)

    prompt_text = db.Column(db.Text, nullable=False)
    frequency = db.Column(db.String(16), nullable=False, default="daily")  # daily|weekly
    day_of_week = db.Column(db.Integer, nullable=True)  # 0=Monday..6=Sunday, weekly only
    hour_utc = db.Column(db.Integer, nullable=False, default=9)  # 0-23

    enabled = db.Column(db.Boolean, default=True)
    last_run_at = db.Column(db.DateTime, nullable=True)
    next_run_at = db.Column(db.DateTime, nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=_now)

    room = db.relationship("ChatRoom")

    def to_dict(self):
        return {
            "id": self.id,
            "room_id": self.room_id,
            "prompt_text": self.prompt_text,
            "frequency": self.frequency,
            "day_of_week": self.day_of_week,
            "hour_utc": self.hour_utc,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "created_at": self.created_at.isoformat(),
        }
