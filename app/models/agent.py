"""
Agents and their execution runs.
An Agent is a named, configured AI worker assigned to Tasks.
An AgentRun is one execution attempt — it has its own Session for context tracking.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class Agent(db.Model):
    __tablename__ = "agents"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=True)
    system_prompt = db.Column(db.Text, nullable=True)   # agent persona / instructions

    # Which tools this agent is allowed to use (JSON list of tool names)
    allowed_tools = db.Column(db.Text, default="[]")    # JSON

    # LLM config override (null = use global config)
    llm_model_override = db.Column(db.String(128), nullable=True)
    max_iterations = db.Column(db.Integer, default=20)  # safety cap on tool loop

    status = db.Column(db.String(32), default="idle")  # idle|running|error
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    runs = db.relationship("AgentRun", back_populates="agent",
                            cascade="all, delete-orphan")

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "allowed_tools": json.loads(self.allowed_tools or "[]"),
            "max_iterations": self.max_iterations,
            "status": self.status,
        }


class AgentRun(db.Model):
    """One execution of an agent against a task."""
    __tablename__ = "agent_runs"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    agent_id = db.Column(db.String(36), db.ForeignKey("agents.id"), nullable=False)
    task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=False)

    # Each run gets its own session for full context tracking
    session_id = db.Column(db.String(36), db.ForeignKey("sessions.id"), nullable=True)

    status = db.Column(db.String(32), default="pending")
    # pending|running|awaiting_approval|completed|failed|cancelled

    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)

    # Outcome
    result_summary = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    iterations_used = db.Column(db.Integer, default=0)
    tokens_used = db.Column(db.Integer, default=0)

    agent = db.relationship("Agent", back_populates="runs")
    task = db.relationship("Task", back_populates="agent_runs")
    steps = db.relationship("AgentStep", back_populates="run",
                             cascade="all, delete-orphan", order_by="AgentStep.step_number")

    def to_dict(self):
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result_summary": self.result_summary,
            "iterations_used": self.iterations_used,
            "tokens_used": self.tokens_used,
            "step_count": len(self.steps),
        }


class AgentStep(db.Model):
    """One step in an agent run: reasoning → tool call → result."""
    __tablename__ = "agent_steps"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    run_id = db.Column(db.String(36), db.ForeignKey("agent_runs.id"), nullable=False)
    step_number = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=_now)

    # What the LLM decided to do
    reasoning = db.Column(db.Text, nullable=True)
    tool_name = db.Column(db.String(128), nullable=True)
    tool_input = db.Column(db.Text, nullable=True)   # JSON
    tool_output = db.Column(db.Text, nullable=True)  # JSON

    # Approval tracking (Tier 2/3 tools)
    required_approval = db.Column(db.Boolean, default=False)
    approved = db.Column(db.Boolean, nullable=True)  # None=pending, True=approved, False=rejected
    approved_at = db.Column(db.DateTime, nullable=True)

    run = db.relationship("AgentRun", back_populates="steps")
