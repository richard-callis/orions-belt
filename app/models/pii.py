"""
PII hash map — local only, never leaves the machine.
Maps SHA-256 hashes back to original values for local recovery.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class PIIHashEntry(db.Model):
    __tablename__ = "pii_hash_map"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    hash_token = db.Column(db.String(16), nullable=False, unique=True)
    # The token as it appears in the sanitized text: e.g., "a3f9c2d1"
    # Full hash kept separately for verification

    full_hash = db.Column(db.String(64), nullable=False)        # SHA-256 hex
    original_value = db.Column(db.Text, nullable=False)          # Fernet-encrypted original
    entity_type = db.Column(db.String(64), nullable=False)       # PERSON, EMAIL, SSN, etc.
    detection_source = db.Column(db.String(32), nullable=True)   # presidio|ner|llm_judge

    # Context: which session/message triggered this
    session_id = db.Column(db.String(36), nullable=True)
    message_id = db.Column(db.String(36), nullable=True)

    created_at = db.Column(db.DateTime, default=_now)
    last_seen_at = db.Column(db.DateTime, default=_now)
    occurrence_count = db.Column(db.Integer, default=1)

    def formatted_token(self):
        """Returns the token as it appears inline: [PII:PERSON:a3f9c2d1]"""
        return f"[PII:{self.entity_type}:{self.hash_token}]"


class PIIException(db.Model):
    """Human-approved false-positive allowlist: a span matching one of these
    entries is skipped by future PII scans instead of being tokenized.

    Anti-poisoning: an exception can only be created by referencing an
    EXISTING PIIHashEntry.hash_token (a real prior detection) — never from
    arbitrary freeform text. This model is never exposed as an MCP tool, so
    no LLM/agent can create, list, or delete exceptions; only the human via
    the Settings UI.
    """
    __tablename__ = "pii_exceptions"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    entity_type = db.Column(db.String(64), nullable=False)
    match_mode = db.Column(db.String(16), nullable=False, default="exact")  # exact|normalized
    value = db.Column(db.Text, nullable=False)  # Fernet-encrypted, mirrors PIIHashEntry.original_value
    source_hash_token = db.Column(db.String(16), nullable=True)  # the detection this was raised from
    created_at = db.Column(db.DateTime, default=_now)

    def decrypted_value(self) -> str:
        from app.services.crypto import decrypt_data
        return decrypt_data(self.value) or ""

    def to_dict(self):
        return {
            "id": self.id,
            "entity_type": self.entity_type,
            "match_mode": self.match_mode,
            "value": self.decrypted_value(),
            "source_hash_token": self.source_hash_token,
            "created_at": self.created_at.isoformat(),
        }
