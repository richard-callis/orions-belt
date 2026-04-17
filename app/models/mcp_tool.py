"""
MCP Tools — built-in and LLM-proposed.
Proposed tools go through: AST scan → local LLM safety review → human approval.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class MCPTool(db.Model):
    __tablename__ = "mcp_tools"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    name = db.Column(db.String(128), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)

    # builtin | proposed | approved | rejected
    source = db.Column(db.String(32), default="builtin")

    # Authorization tier: 0=auto, 1=auto+audit, 2=warn, 3=hard_stop
    tier = db.Column(db.Integer, default=1)

    # The Python function body (for proposed tools only)
    code = db.Column(db.Text, nullable=True)

    # Input schema (JSON Schema) — describes what args the tool accepts
    input_schema = db.Column(db.Text, default="{}")  # JSON

    enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    proposals = db.relationship("ToolProposal", back_populates="tool",
                                 cascade="all, delete-orphan")

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "tier": self.tier,
            "input_schema": json.loads(self.input_schema or "{}"),
            "enabled": self.enabled,
            "has_code": bool(self.code),
        }


class ToolProposal(db.Model):
    """
    Audit trail for every tool proposal:
    - proposed code
    - AST scan result
    - local LLM safety review
    - human decision (approve/reject)
    """
    __tablename__ = "tool_proposals"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    tool_id = db.Column(db.String(36), db.ForeignKey("mcp_tools.id"), nullable=False)

    proposed_by = db.Column(db.String(128), nullable=True)  # agent name or "user"
    proposed_at = db.Column(db.DateTime, default=_now)
    proposed_code = db.Column(db.Text, nullable=False)
    proposed_description = db.Column(db.Text, nullable=True)

    # AST scan
    ast_passed = db.Column(db.Boolean, nullable=True)
    ast_findings = db.Column(db.Text, nullable=True)  # JSON list of issues

    # Local LLM safety review
    llm_review_passed = db.Column(db.Boolean, nullable=True)
    llm_review_summary = db.Column(db.Text, nullable=True)

    # Human decision
    decision = db.Column(db.String(16), nullable=True)  # approved|rejected|pending
    decided_at = db.Column(db.DateTime, nullable=True)
    decision_notes = db.Column(db.Text, nullable=True)

    tool = db.relationship("MCPTool", back_populates="proposals")
