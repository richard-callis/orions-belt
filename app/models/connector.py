"""
Connectors: REST APIs, Outlook (COM), SQL Server.
Each connector is a named, configured integration that agents and MCP tools can call.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class Connector(db.Model):
    __tablename__ = "connectors"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    name = db.Column(db.String(128), nullable=False, unique=True)
    connector_type = db.Column(db.String(32), nullable=False)
    # rest_api | outlook | sql_server

    description = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    # Config stored as JSON (type-specific fields)
    config = db.Column(db.Text, default="{}")  # JSON

    # Auth stored encrypted
    auth_config = db.Column(db.Text, nullable=True)  # Fernet-encrypted JSON

    def to_dict(self, include_auth=False):
        import json
        d = {
            "id": self.id,
            "name": self.name,
            "connector_type": self.connector_type,
            "description": self.description,
            "enabled": self.enabled,
            "config": json.loads(self.config or "{}"),
        }
        return d


class AuthorizedDirectory(db.Model):
    """
    Directories explicitly whitelisted for MCP file operations.
    No file op can touch a path outside an authorized directory.
    """
    __tablename__ = "authorized_directories"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    path = db.Column(db.String(1024), nullable=False, unique=True)
    alias = db.Column(db.String(128), nullable=False)   # LLM sees this, not the raw path
    recursive = db.Column(db.Boolean, default=True)
    read_only = db.Column(db.Boolean, default=False)    # forces Tier 0 only
    max_tier = db.Column(db.Integer, default=3)         # cap effective tier (0-3)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    authorized_at = db.Column(db.DateTime, default=_now)
    expires_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.String(512), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "path": self.path,
            "alias": self.alias,
            "recursive": self.recursive,
            "read_only": self.read_only,
            "max_tier": self.max_tier,
            "authorized_at": self.authorized_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
