"""Connectors — CRUD API + connectivity test for REST API, SQL Server, and Outlook connectors."""
import json
import logging

from flask import Blueprint, jsonify, render_template, request

from app import db
from app.models.connector import Connector

bp = Blueprint("connectors", __name__, url_prefix="/connectors")
log = logging.getLogger("orions-belt")


@bp.route("/")
@bp.route("")
def index():
    return render_template("connectors.html")


# ── List ──────────────────────────────────────────────────────────────────────

@bp.route("/api/connectors", methods=["GET"])
def list_connectors():
    connectors = Connector.query.order_by(Connector.name).all()
    return jsonify([c.to_dict() for c in connectors])


# ── Create ────────────────────────────────────────────────────────────────────

@bp.route("/api/connectors", methods=["POST"])
def create_connector():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    connector_type = (body.get("connector_type") or "").strip()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if connector_type not in ("rest_api", "sql_server", "outlook"):
        return jsonify({"error": "connector_type must be rest_api, sql_server, or outlook"}), 400
    if Connector.query.filter_by(name=name).first():
        return jsonify({"error": f"Connector '{name}' already exists"}), 409

    connector = Connector(
        name=name,
        connector_type=connector_type,
        description=body.get("description", ""),
        config=json.dumps(body.get("config", {})),
        enabled=bool(body.get("enabled", True)),
    )
    auth = body.get("auth", {})
    if auth:
        connector.set_auth(auth)

    db.session.add(connector)
    db.session.commit()
    log.info("Connector created: name=%r type=%s id=%s", name, connector_type, connector.id)
    return jsonify(connector.to_dict()), 201


# ── Get ───────────────────────────────────────────────────────────────────────

@bp.route("/api/connectors/<connector_id>", methods=["GET"])
def get_connector(connector_id):
    c = Connector.query.get(connector_id)
    if not c:
        return jsonify({"error": "Connector not found"}), 404
    return jsonify(c.to_dict())


# ── Update ────────────────────────────────────────────────────────────────────

@bp.route("/api/connectors/<connector_id>", methods=["PATCH"])
def update_connector(connector_id):
    c = Connector.query.get(connector_id)
    if not c:
        return jsonify({"error": "Connector not found"}), 404

    body = request.get_json() or {}
    if "name" in body:
        new_name = (body["name"] or "").strip()
        if not new_name:
            return jsonify({"error": "name cannot be empty"}), 400
        existing = Connector.query.filter_by(name=new_name).first()
        if existing and existing.id != connector_id:
            return jsonify({"error": f"Connector '{new_name}' already exists"}), 409
        c.name = new_name
    if "description" in body:
        c.description = body["description"]
    if "enabled" in body:
        c.enabled = bool(body["enabled"])
    if "config" in body and isinstance(body["config"], dict):
        c.config = json.dumps(body["config"])
    if "auth" in body and isinstance(body["auth"], dict):
        # Only update auth if caller sent a non-empty dict that isn't all masked values
        auth = body["auth"]
        has_real_values = any(
            v and not str(v).startswith("*")
            for v in auth.values()
        )
        if has_real_values:
            c.set_auth(auth)

    db.session.commit()
    return jsonify(c.to_dict())


# ── Delete ────────────────────────────────────────────────────────────────────

@bp.route("/api/connectors/<connector_id>", methods=["DELETE"])
def delete_connector(connector_id):
    c = Connector.query.get(connector_id)
    if not c:
        return jsonify({"error": "Connector not found"}), 404
    db.session.delete(c)
    db.session.commit()
    log.info("Connector deleted: id=%s name=%r", connector_id, c.name)
    return "", 204


# ── Test ──────────────────────────────────────────────────────────────────────

@bp.route("/api/connectors/<connector_id>/test", methods=["POST"])
def test_connector(connector_id):
    """Test connectivity for the given connector. Returns {ok, message}."""
    c = Connector.query.get(connector_id)
    if not c:
        return jsonify({"error": "Connector not found"}), 404

    try:
        if c.connector_type == "rest_api":
            return _test_rest_api(c)
        elif c.connector_type == "sql_server":
            return _test_sql_server(c)
        elif c.connector_type == "outlook":
            return _test_outlook(c)
        else:
            return jsonify({"ok": False, "message": f"Unknown type: {c.connector_type}"}), 400
    except Exception as e:
        log.warning("Connector test failed id=%s: %s", connector_id, e)
        return jsonify({"ok": False, "message": str(e)}), 200


def _test_rest_api(c: Connector):
    cfg = json.loads(c.config or "{}")
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        return jsonify({"ok": False, "message": "No base_url configured"}), 200

    import httpx
    auth = c.get_auth()
    headers = {}
    auth_type = cfg.get("auth_type", "none")
    if auth_type == "bearer" and auth.get("token"):
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif auth_type == "api_key" and auth.get("api_key"):
        headers[auth.get("header_name", "X-API-Key")] = auth["api_key"]
    elif auth_type == "basic" and auth.get("username"):
        import base64
        creds = base64.b64encode(f"{auth['username']}:{auth.get('password', '')}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(base_url, headers=headers)
        return jsonify({"ok": resp.status_code < 500, "message": f"HTTP {resp.status_code}"})
    except httpx.ConnectError as e:
        return jsonify({"ok": False, "message": f"Connection refused: {e}"})
    except httpx.TimeoutException:
        return jsonify({"ok": False, "message": "Connection timed out (10s)"})


def _test_sql_server(c: Connector):
    cfg = json.loads(c.config or "{}")
    server = cfg.get("server", "")
    database = cfg.get("database", "")
    if not server:
        return jsonify({"ok": False, "message": "No server configured"}), 200

    try:
        import pyodbc
    except ImportError:
        return jsonify({"ok": False, "message": "pyodbc not installed — run: pip install pyodbc"}), 200

    auth = c.get_auth()
    auth_type = cfg.get("auth_type", "windows")
    if auth_type == "windows":
        conn_str = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes"
    else:
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
            f"DATABASE={database};UID={auth.get('username', '')};PWD={auth.get('password', '')}"
        )

    try:
        conn = pyodbc.connect(conn_str, timeout=5)
        conn.close()
        return jsonify({"ok": True, "message": f"Connected to {server}/{database}"})
    except pyodbc.Error as e:
        return jsonify({"ok": False, "message": str(e)})


def _test_outlook(c: Connector):
    try:
        import win32com.client  # noqa: F401
        return jsonify({"ok": True, "message": "win32com available — Outlook connector ready"})
    except ImportError:
        return jsonify({"ok": False, "message": "pywin32 not installed (Windows only) — run: pip install pywin32"})
