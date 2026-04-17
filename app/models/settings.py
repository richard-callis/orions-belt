"""
App-wide key-value settings store.
Used for LLM config, PII model path, first-run state, etc.
"""
import json
from datetime import datetime, timezone
from app import db


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(128), primary_key=True)
    value = db.Column(db.Text, nullable=True)
    value_type = db.Column(db.String(16), default="string")  # string|json|bool|int
    description = db.Column(db.String(256), nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    @classmethod
    def get(cls, key: str, default=None):
        row = cls.query.get(key)
        if row is None:
            return default
        if row.value_type == "json":
            return json.loads(row.value) if row.value else default
        if row.value_type == "bool":
            return row.value == "true"
        if row.value_type == "int":
            return int(row.value) if row.value else default
        return row.value

    @classmethod
    def set(cls, key: str, value, value_type: str = "string", description: str = None):
        if value_type == "json":
            value = json.dumps(value)
        elif value_type == "bool":
            value = "true" if value else "false"
        else:
            value = str(value) if value is not None else None

        row = cls.query.get(key)
        if row:
            row.value = value
            row.value_type = value_type
            if description:
                row.description = description
        else:
            row = cls(key=key, value=value, value_type=value_type, description=description)
            db.session.add(row)
        db.session.commit()

    @classmethod
    def is_setup_complete(cls) -> bool:
        return cls.get("setup.completed", default=False, ) == "true" or \
               cls.get("setup.completed") is True
