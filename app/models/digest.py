"""
Scheduled digest emails — a periodic summary of agent/LLM activity sent to
a recipient. Deliberately a SEPARATE table from ScheduledTrigger, not a
trigger_type discriminator on it: ScheduledTrigger's room_id/prompt_text
are NOT NULL, and this app's schema migration (launch.py::_migrate_schema)
can only ADD columns to an existing table, never relax a NOT NULL
constraint — making those nullable would work on a fresh test DB (built
from the current model) and break on every existing install (the on-disk
table keeps its original constraint). A new table sidesteps that entirely;
create_all() handles it with zero migration risk.

See app/services/digest.py for the dispatch loop, which reuses
app/services/triggers.py's generic run_due_schedules helper (the same
claim-then-dispatch scheduling logic, not a second copy of it).
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class DigestSchedule(db.Model):
    __tablename__ = "digest_schedules"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    recipient_email = db.Column(db.String(320), nullable=False)

    frequency = db.Column(db.String(16), nullable=False, default="weekly")  # daily|weekly
    day_of_week = db.Column(db.Integer, nullable=True)  # 0=Monday..6=Sunday, weekly only
    hour_utc = db.Column(db.Integer, nullable=False, default=9)  # 0-23

    enabled = db.Column(db.Boolean, default=True)
    last_run_at = db.Column(db.DateTime, nullable=True)
    next_run_at = db.Column(db.DateTime, nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self):
        return {
            "id": self.id,
            "recipient_email": self.recipient_email,
            "frequency": self.frequency,
            "day_of_week": self.day_of_week,
            "hour_utc": self.hour_utc,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "created_at": self.created_at.isoformat(),
        }
