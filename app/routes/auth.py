"""Authentication routes — login, logout, session management."""
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, make_response
from app import db
from app.models.settings import Setting

bp = Blueprint("auth", __name__)
log = logging.getLogger("orions-belt")


@bp.route("/api/auth/login", methods=["POST"])
def login():
    """Authenticate with username/password, return session token."""
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    stored_user = Setting.get("auth.username")
    stored_hash = Setting.get("auth.password_hash")

    if not stored_user or not stored_hash:
        return jsonify({"error": "No credentials configured — complete first-run setup"}), 403

    if username != stored_user:
        return jsonify({"error": "Invalid credentials"}), 401

    from werkzeug.security import check_password_hash
    if not check_password_hash(stored_hash, password):
        return jsonify({"error": "Invalid credentials"}), 401

    # Generate session token
    import secrets
    token = secrets.token_hex(32)
    Setting.set("auth.session_token", token)
    db.session.commit()

    resp = make_response(jsonify({"ok": True, "username": username}))
    resp.set_cookie("orions_belt_session", token, httponly=True, samesite="Lax", max_age=86400 * 7)
    return resp


@bp.route("/api/auth/logout", methods=["POST"])
def logout():
    """Clear session."""
    Setting.set("auth.session_token", "")
    db.session.commit()
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("orions_belt_session")
    return resp


@bp.route("/api/auth/status")
def auth_status():
    """Check current auth status."""
    from flask import g
    user = getattr(g, "current_user", None)
    return jsonify({"authenticated": user is not None, "username": user})
