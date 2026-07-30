"""
Scheduled digest emails — CRUD for DigestSchedule. See app/services/digest.py
for the dispatch loop and app/models/digest.py for why this is a separate
table from ScheduledTrigger.
"""
import logging
import re

from flask import Blueprint, jsonify, request

from app import db
from app.models.digest import DigestSchedule

bp = Blueprint("digest_schedules", __name__, url_prefix="/api/digest-schedules")
log = logging.getLogger("orions-belt")

_VALID_FREQUENCIES = ("daily", "weekly")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.route("", methods=["GET"])
def list_digest_schedules():
    schedules = DigestSchedule.query.order_by(DigestSchedule.created_at.desc()).all()
    return jsonify([s.to_dict() for s in schedules])


@bp.route("", methods=["POST"])
def create_digest_schedule():
    from app.services.triggers import _compute_next_run

    body = request.get_json() or {}
    recipient_email = (body.get("recipient_email") or "").strip()
    if not recipient_email:
        return jsonify({"error": "recipient_email is required"}), 400
    if not _EMAIL_RE.match(recipient_email):
        return jsonify({"error": "recipient_email is not a valid email address"}), 400

    frequency = body.get("frequency", "weekly")
    if frequency not in _VALID_FREQUENCIES:
        return jsonify({"error": f"frequency must be one of {_VALID_FREQUENCIES}"}), 400

    day_of_week = body.get("day_of_week")
    if frequency == "weekly":
        if day_of_week is None:
            return jsonify({"error": "day_of_week is required for weekly digests (0=Monday..6=Sunday)"}), 400
        try:
            day_of_week = int(day_of_week)
        except (TypeError, ValueError):
            return jsonify({"error": "day_of_week must be an integer 0-6"}), 400
        if not (0 <= day_of_week <= 6):
            return jsonify({"error": "day_of_week must be 0-6"}), 400
    else:
        day_of_week = None

    try:
        hour_utc = int(body.get("hour_utc", 9))
    except (TypeError, ValueError):
        return jsonify({"error": "hour_utc must be an integer 0-23"}), 400
    if not (0 <= hour_utc <= 23):
        return jsonify({"error": "hour_utc must be 0-23"}), 400

    schedule = DigestSchedule(
        recipient_email=recipient_email, frequency=frequency,
        day_of_week=day_of_week, hour_utc=hour_utc, enabled=bool(body.get("enabled", True)),
    )
    schedule.next_run_at = _compute_next_run(frequency, day_of_week, hour_utc)
    db.session.add(schedule)
    db.session.commit()
    return jsonify(schedule.to_dict()), 201


@bp.route("/<schedule_id>", methods=["PATCH"])
def update_digest_schedule(schedule_id):
    from app.services.triggers import _compute_next_run

    schedule = DigestSchedule.query.get(schedule_id)
    if not schedule:
        return jsonify({"error": "Digest schedule not found"}), 404

    body = request.get_json() or {}
    if "recipient_email" in body:
        recipient_email = (body["recipient_email"] or "").strip()
        if not recipient_email:
            return jsonify({"error": "recipient_email cannot be empty"}), 400
        if not _EMAIL_RE.match(recipient_email):
            return jsonify({"error": "recipient_email is not a valid email address"}), 400
        schedule.recipient_email = recipient_email
    if "frequency" in body:
        if body["frequency"] not in _VALID_FREQUENCIES:
            return jsonify({"error": f"frequency must be one of {_VALID_FREQUENCIES}"}), 400
        schedule.frequency = body["frequency"]
    if "day_of_week" in body:
        day_of_week = body["day_of_week"]
        if day_of_week is not None:
            try:
                day_of_week = int(day_of_week)
            except (TypeError, ValueError):
                return jsonify({"error": "day_of_week must be an integer 0-6"}), 400
            if not (0 <= day_of_week <= 6):
                return jsonify({"error": "day_of_week must be 0-6"}), 400
        schedule.day_of_week = day_of_week
    if "hour_utc" in body:
        try:
            hour_utc = int(body["hour_utc"])
        except (TypeError, ValueError):
            return jsonify({"error": "hour_utc must be an integer 0-23"}), 400
        if not (0 <= hour_utc <= 23):
            return jsonify({"error": "hour_utc must be 0-23"}), 400
        schedule.hour_utc = hour_utc
    if "enabled" in body:
        schedule.enabled = bool(body["enabled"])

    if schedule.frequency == "weekly" and schedule.day_of_week is None:
        return jsonify({"error": "day_of_week is required for weekly digests"}), 400

    schedule.next_run_at = _compute_next_run(schedule.frequency, schedule.day_of_week, schedule.hour_utc)
    db.session.commit()
    return jsonify(schedule.to_dict())


@bp.route("/<schedule_id>", methods=["DELETE"])
def delete_digest_schedule(schedule_id):
    schedule = DigestSchedule.query.get(schedule_id)
    if not schedule:
        return jsonify({"error": "Digest schedule not found"}), 404
    db.session.delete(schedule)
    db.session.commit()
    return "", 204
