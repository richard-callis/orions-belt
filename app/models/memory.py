"""
Persistent memory — cross-session facts, per-project context, entity knowledge.
Embeddings stored as binary blobs for cosine similarity recall.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class Memory(db.Model):
    __tablename__ = "memories"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)

    # persistent | project | entity
    memory_type = db.Column(db.String(32), default="persistent")

    # Optional scope — links memory to specific work items or connectors
    scope_project_id = db.Column(db.String(36), db.ForeignKey("projects.id"), nullable=True)
    scope_epic_id = db.Column(db.String(36), db.ForeignKey("epics.id"), nullable=True)
    scope_task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=True)
    scope_connector_id = db.Column(db.String(36), db.ForeignKey("connectors.id"), nullable=True)

    title = db.Column(db.String(512), nullable=False)
    content = db.Column(db.Text, nullable=False)

    # sentence-transformers embedding — numpy array serialized to bytes
    embedding = db.Column(db.LargeBinary, nullable=True)

    # Who wrote this memory
    source = db.Column(db.String(64), default="user")  # user | agent | system

    pinned = db.Column(db.Boolean, default=False)   # pinned memories always injected
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            "id": self.id,
            "memory_type": self.memory_type,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "pinned": self.pinned,
            "scope_project_id": self.scope_project_id,
            "scope_epic_id": self.scope_epic_id,
            "scope_task_id": self.scope_task_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
