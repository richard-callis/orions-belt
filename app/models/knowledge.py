"""Knowledge base: notes, wikis, runbooks, and llm-context notes auto-injected into agents."""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class Note(db.Model):
    __tablename__ = "notes"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    title = db.Column(db.String(512), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    # note|wiki|runbook|llm-context
    note_type = db.Column(db.String(32), default="note")
    project_id = db.Column(db.String(36), db.ForeignKey("projects.id"), nullable=True)
    pinned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    project = db.relationship("Project", foreign_keys=[project_id])

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "note_type": self.note_type,
            "project_id": self.project_id,
            "pinned": self.pinned,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
