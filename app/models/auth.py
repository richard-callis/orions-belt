"""User model for authentication and session management."""
from datetime import datetime, timezone
from app import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(128), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(32), default="admin")  # admin | user | readonly
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = db.Column(db.DateTime, nullable=True)
    active = db.Column(db.Boolean, default=True)

    def check_password(self, password: str) -> bool:
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

    @staticmethod
    def hash_password(password: str) -> str:
        from werkzeug.security import generate_password_hash
        return generate_password_hash(password)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }
