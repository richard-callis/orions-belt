"""
Dream — LLM-driven lessons-learned extraction (see app/services/dream.py).

A DreamLesson is a staging row, not a live Memory: nothing extracted by the
periodic extraction pass is recalled by agents until a human approves it —
an LLM writing unattended to global agent context (every approved lesson is
auto-injected into every future room reply via the same recall path as any
other memory) is the one genuinely new autonomous-write risk this feature
adds, so review is not optional/configurable away to "auto-approve".
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class DreamLesson(db.Model):
    __tablename__ = "dream_lessons"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    title = db.Column(db.String(120), nullable=False)
    content = db.Column(db.String(500), nullable=False)
    folder = db.Column(db.String(64), nullable=True)  # LLM's free-text category, display-only

    status = db.Column(db.String(16), default="pending")  # pending|approved|rejected
    memory_id = db.Column(db.String(36), db.ForeignKey("memories.id"), nullable=True)  # set once approved

    created_at = db.Column(db.DateTime, default=_now)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "folder": self.folder,
            "status": self.status,
            "memory_id": self.memory_id,
            "created_at": self.created_at.isoformat(),
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
        }
