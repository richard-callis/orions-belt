"""
Structured log tables — four streams: Audit, PII, Agent, LLM.
All queryable from the UI log viewer.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class AuditLog(db.Model):
    """Every MCP tool call — tier, outcome, who called it."""
    __tablename__ = "audit_logs"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    tool_name = db.Column(db.String(128), nullable=False)
    tier = db.Column(db.Integer, nullable=False)
    caller = db.Column(db.String(128), nullable=True)        # agent name or "user"
    session_id = db.Column(db.String(36), nullable=True)
    run_id = db.Column(db.String(36), nullable=True)

    # What was attempted
    input_summary = db.Column(db.Text, nullable=True)        # sanitized (no PII)
    target_path = db.Column(db.String(1024), nullable=True)  # file path if applicable

    # Outcome
    outcome = db.Column(db.String(32), nullable=False)       # auto|approved|rejected|blocked
    result_summary = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)


class PIILog(db.Model):
    """Every PII/PHI detection event."""
    __tablename__ = "pii_logs"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    session_id = db.Column(db.String(36), nullable=True)
    message_id = db.Column(db.String(36), nullable=True)
    direction = db.Column(db.String(8), default="outbound")  # outbound | inbound

    entities_found = db.Column(db.Integer, default=0)
    entity_types = db.Column(db.String(256), nullable=True)  # comma-separated
    detection_sources = db.Column(db.String(128), nullable=True)  # presidio,ner,llm
    hashes_created = db.Column(db.Integer, default=0)


class AgentLog(db.Model):
    """Per-step agent execution log."""
    __tablename__ = "agent_logs"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    run_id = db.Column(db.String(36), nullable=False, index=True)
    step_number = db.Column(db.Integer, nullable=False)
    agent_name = db.Column(db.String(128), nullable=True)
    task_id = db.Column(db.String(36), nullable=True)

    event = db.Column(db.String(64), nullable=False)
    # started|tool_call|tool_result|approval_required|completed|failed

    detail = db.Column(db.Text, nullable=True)
    tokens_used = db.Column(db.Integer, default=0)


class LLMLog(db.Model):
    """Every external LLM API call."""
    __tablename__ = "llm_logs"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    provider = db.Column(db.String(64), nullable=True)       # openai | llamaserver | custom
    model = db.Column(db.String(128), nullable=True)
    session_id = db.Column(db.String(36), nullable=True)
    run_id = db.Column(db.String(36), nullable=True)

    tokens_in = db.Column(db.Integer, default=0)
    tokens_out = db.Column(db.Integer, default=0)
    latency_ms = db.Column(db.Integer, default=0)

    # Estimated cost in USD (null if unknown/local)
    estimated_cost_usd = db.Column(db.Float, nullable=True)

    success = db.Column(db.Boolean, default=True)
    error = db.Column(db.Text, nullable=True)
