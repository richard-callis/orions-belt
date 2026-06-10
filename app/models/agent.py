"""
Agents and their execution runs.
An Agent is a named, configured AI worker assigned to Tasks.
An AgentRun is one execution attempt.
"""
import json
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
    system_prompt = db.Column(db.Text, nullable=True)
    allowed_tools = db.Column(db.Text, default="[]")
    llm_model_override = db.Column(db.String(128), nullable=True)
    max_iterations = db.Column(db.Integer, default=20)
    status = db.Column(db.String(32), default="idle")  # idle|running|error

    # Token budget constraints (null = unlimited)
    daily_token_budget = db.Column(db.Integer, nullable=True)
    monthly_token_budget = db.Column(db.Integer, nullable=True)

    # Role hint for tool scoping: auto|deployment|investigation|knowledge|coordination
    role_scope = db.Column(db.String(32), nullable=True)

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    runs = db.relationship("AgentRun", back_populates="agent",
                            cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt or "",
            "allowed_tools": json.loads(self.allowed_tools or "[]"),
            "llm_model_override": self.llm_model_override,
            "max_iterations": self.max_iterations,
            "status": self.status,
            "daily_token_budget": self.daily_token_budget,
            "monthly_token_budget": self.monthly_token_budget,
            "role_scope": self.role_scope,
        }


class AgentRun(db.Model):
    """One execution of an agent against a task."""
    __tablename__ = "agent_runs"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    agent_id = db.Column(db.String(36), db.ForeignKey("agents.id"), nullable=False)
    task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=False)
    session_id = db.Column(db.String(36), db.ForeignKey("sessions.id"), nullable=True)

    # pending|running|awaiting_approval|pending_validation|completed|failed|cancelled
    status = db.Column(db.String(32), default="pending")

    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)

    result_summary = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    iterations_used = db.Column(db.Integer, default=0)
    tokens_used = db.Column(db.Integer, default=0)

    # Plan-before-execute
    plan_xml = db.Column(db.Text, nullable=True)
    plan_approved = db.Column(db.Boolean, nullable=True)
    blocked_steps_json = db.Column(db.Text, default="[]")

    # Reliability tracking
    reviewer_verdict = db.Column(db.String(32), nullable=True)  # approved|rejected|skipped
    remediation_attempts = db.Column(db.Integer, default=0)

    agent = db.relationship("Agent", back_populates="runs")
    task = db.relationship("Task", back_populates="agent_runs")
    steps = db.relationship("AgentStep", back_populates="run",
                             cascade="all, delete-orphan", order_by="AgentStep.step_number")

    def to_dict(self):
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "task_title": self.task.title if self.task else None,
            "session_id": self.session_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result_summary": self.result_summary,
            "error_message": self.error_message,
            "iterations_used": self.iterations_used,
            "tokens_used": self.tokens_used,
            "step_count": len(self.steps),
            "plan_approved": self.plan_approved,
            "reviewer_verdict": self.reviewer_verdict,
            "remediation_attempts": self.remediation_attempts,
        }


class AgentStep(db.Model):
    """One step in an agent run: reasoning → tool call → result."""
    __tablename__ = "agent_steps"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    run_id = db.Column(db.String(36), db.ForeignKey("agent_runs.id"), nullable=False)
    step_number = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=_now)

    reasoning = db.Column(db.Text, nullable=True)
    tool_name = db.Column(db.String(128), nullable=True)
    tool_input = db.Column(db.Text, nullable=True)
    tool_output = db.Column(db.Text, nullable=True)

    required_approval = db.Column(db.Boolean, default=False)
    approved = db.Column(db.Boolean, nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)

    # Idempotency: SHA-256 of (tool_name + canonical args)
    checkpoint_hash = db.Column(db.String(64), nullable=True)
    is_checkpointed = db.Column(db.Boolean, default=False)
    blocked = db.Column(db.Boolean, default=False)

    run = db.relationship("AgentRun", back_populates="steps")


class TokenUsage(db.Model):
    """Per-run token consumption for daily/monthly budget enforcement."""
    __tablename__ = "token_usage"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    agent_id = db.Column(db.String(36), db.ForeignKey("agents.id"), nullable=False)
    run_id = db.Column(db.String(36), db.ForeignKey("agent_runs.id"), nullable=True)
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    period_day = db.Column(db.String(10), nullable=False)   # "YYYY-MM-DD"
    period_month = db.Column(db.String(7), nullable=False)  # "YYYY-MM"
    created_at = db.Column(db.DateTime, default=_now)
