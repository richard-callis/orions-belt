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
    original_value = db.Column(db.Text, nullable=False)          # plaintext original
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
