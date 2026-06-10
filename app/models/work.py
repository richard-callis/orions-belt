"""
Work hierarchy: Project → Epic → Feature → Task
Agents are assigned to Tasks.
"""
import json
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
    folder_path = db.Column(db.String(1024), nullable=True)
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
            "folder_path": self.folder_path,
            "created_at": self.created_at.isoformat(),
            "epic_count": len(self.epics),
        }


class Epic(db.Model):
    __tablename__ = "epics"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(36), db.ForeignKey("projects.id"), nullable=False)
    title = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text, nullable=True)
    plan = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), default="backlog")
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
            "plan": self.plan,
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
    plan = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), default="backlog")
    priority = db.Column(db.Integer, default=0)
    plan_approved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    epic = db.relationship("Epic", back_populates="features")
    tasks = db.relationship(
        "Task", back_populates="feature",
        cascade="all, delete-orphan",
        order_by="Task.wave",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "epic_id": self.epic_id,
            "title": self.title,
            "description": self.description,
            "plan": self.plan,
            "status": self.status,
            "priority": self.priority,
            "plan_approved_at": self.plan_approved_at.isoformat() if self.plan_approved_at else None,
            "task_count": len(self.tasks),
        }


class Task(db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    feature_id = db.Column(db.String(36), db.ForeignKey("features.id"), nullable=False)
    title = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text, nullable=True)
    acceptance_criteria = db.Column(db.Text, nullable=True)
    plan = db.Column(db.Text, nullable=True)
    # backlog|in_progress|review|done|blocked|pending_validation|cancelled
    status = db.Column(db.String(32), default="backlog")
    priority = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    assigned_agent_id = db.Column(db.String(36), db.ForeignKey("agents.id"), nullable=True)

    # Execution ordering
    depends_on_json = db.Column(db.Text, default="[]")
    wave = db.Column(db.Integer, default=0)
    plan_approved_at = db.Column(db.DateTime, nullable=True)
    plan_risk_level = db.Column(db.String(32), nullable=True)  # low|medium|high|critical

    feature = db.relationship("Feature", back_populates="tasks")
    assigned_agent = db.relationship("Agent", foreign_keys=[assigned_agent_id])
    agent_runs = db.relationship("AgentRun", back_populates="task",
                                  cascade="all, delete-orphan")

    @property
    def depends_on(self):
        return json.loads(self.depends_on_json or "[]")

    @depends_on.setter
    def depends_on(self, value):
        self.depends_on_json = json.dumps(value or [])

    def to_dict(self):
        return {
            "id": self.id,
            "feature_id": self.feature_id,
            "title": self.title,
            "description": self.description,
            "acceptance_criteria": self.acceptance_criteria,
            "plan": self.plan,
            "status": self.status,
            "priority": self.priority,
            "assigned_agent_id": self.assigned_agent_id,
            "depends_on": self.depends_on,
            "wave": self.wave,
            "plan_approved_at": self.plan_approved_at.isoformat() if self.plan_approved_at else None,
            "plan_risk_level": self.plan_risk_level,
        }
