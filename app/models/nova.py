"""
Nova — reusable templates for Agents, Connectors, MCP Tools, and Workflows.
Bundled Novas ship with the app; users can create and save their own.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


NOVA_TYPES = ("agent", "connector", "mcp_tool", "workflow")
NOVA_SOURCES = ("bundled", "user")

AGENT_CATEGORIES    = ("Writing", "Analysis", "DevOps", "Data", "Research", "Productivity")
CONNECTOR_CATEGORIES = ("REST API", "Database", "Messaging", "DevTools", "Analytics")
MCP_CATEGORIES      = ("File Ops", "Web", "Code Execution", "Shell", "Data")
WORKFLOW_CATEGORIES  = ("Software", "Data", "Operations", "Research")


class Nova(db.Model):
    __tablename__ = "novas"

    id          = db.Column(db.String(36),  primary_key=True, default=_uuid)
    name        = db.Column(db.String(128), nullable=False, unique=True)   # slug
    display_name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text,        nullable=True)
    nova_type   = db.Column(db.String(32),  nullable=False)   # agent|connector|mcp_tool|workflow
    category    = db.Column(db.String(64),  nullable=True)
    source      = db.Column(db.String(32),  default="bundled")  # bundled|user
    version     = db.Column(db.String(32),  default="1.0.0")
    tags        = db.Column(db.Text,        default="[]")   # JSON array
    config      = db.Column(db.Text,        default="{}")   # JSON — type-specific payload
    created_at  = db.Column(db.DateTime,    default=_now)
    updated_at  = db.Column(db.DateTime,    default=_now, onupdate=_now)

    def to_dict(self):
        import json
        return {
            "id":           self.id,
            "name":         self.name,
            "display_name": self.display_name,
            "description":  self.description,
            "nova_type":    self.nova_type,
            "category":     self.category,
            "source":       self.source,
            "version":      self.version,
            "tags":         json.loads(self.tags  or "[]"),
            "config":       json.loads(self.config or "{}"),
            "created_at":   self.created_at.isoformat() if self.created_at else None,
        }
