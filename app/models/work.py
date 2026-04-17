"""
Work hierarchy: Project → Epic → Feature → Task
Agents are assigned to Tasks.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    name = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), default="active")  # active|paused|completed|archived
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    epics = db.relationship("Epic", back_populates="project",
                             cascade="all, delete-orphan", order_by="Epic.created_at")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "epic_count": len(self.epics),
        }


class Epic(db.Model):
    __tablename__ = "epics"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(36), db.ForeignKey("projects.id"), nullable=False)
    title = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), default="backlog")  # backlog|in_progress|done
    priority = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    project = db.relationship("Project", back_populates="epics")
    features = db.relationship("Feature", back_populates="epic",
                                cascade="all, delete-orphan", order_by="Feature.created_at")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "feature_count": len(self.features),
        }


class Feature(db.Model):
    __tablename__ = "features"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    epic_id = db.Column(db.String(36), db.ForeignKey("epics.id"), nullable=False)
    title = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), default="backlog")
    priority = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    epic = db.relationship("Epic", back_populates="features")
    tasks = db.relationship("Task", back_populates="feature",
                             cascade="all, delete-orphan", order_by="Task.created_at")

    def to_dict(self):
        return {
            "id": self.id,
            "epic_id": self.epic_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "task_count": len(self.tasks),
        }


class Task(db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    feature_id = db.Column(db.String(36), db.ForeignKey("features.id"), nullable=False)
    title = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text, nullable=True)
    acceptance_criteria = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), default="backlog")  # backlog|in_progress|review|done|blocked
    priority = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # Agent assignment
    assigned_agent_id = db.Column(db.String(36), db.ForeignKey("agents.id"), nullable=True)

    feature = db.relationship("Feature", back_populates="tasks")
    assigned_agent = db.relationship("Agent", foreign_keys=[assigned_agent_id])
    agent_runs = db.relationship("AgentRun", back_populates="task",
                                  cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "feature_id": self.feature_id,
            "title": self.title,
            "description": self.description,
            "acceptance_criteria": self.acceptance_criteria,
            "status": self.status,
            "priority": self.priority,
            "assigned_agent_id": self.assigned_agent_id,
        }
