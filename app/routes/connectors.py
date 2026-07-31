"""Connectors — CRUD API + connectivity test for REST API, SQL Server, and Outlook connectors."""
import json
import logging

from flask import Blueprint, jsonify, render_template, request

from app import db
from app.models.connector import Connector
from app.services.oauth_providers import OAUTH_CONNECTOR_TYPES, get_provider_config

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
    _VALID_TYPES = ("rest_api", "sql_server", "outlook", "azure_devops", "github",
                    "google", "microsoft_graph", "salesforce", "jira", "linear")
    if connector_type not in _VALID_TYPES:
        return jsonify({"error": f"connector_type must be one of {_VALID_TYPES}"}), 400
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
        # Merge into the stored auth: masked placeholders ("****", "*…1234")
        # mean "unchanged", so only real values overwrite. This prevents editing
        # one field (e.g. username) from wiping the others' secrets.
        incoming = body["auth"]
        try:
            merged = dict(c.get_auth() or {})
        except Exception:
            merged = {}
        changed = False
        for k, v in incoming.items():
            if v is None or str(v).startswith("*"):
                continue  # masked/empty → keep the stored secret for this field
            merged[k] = v
            changed = True
        if changed:
            c.set_auth(merged)

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
        elif c.connector_type == "azure_devops":
            return _test_azure_devops(c)
        elif c.connector_type == "github":
            return _test_github(c)
        elif c.connector_type == "jira":
            return _test_jira(c)
        elif c.connector_type == "linear":
            return _test_linear(c)
        elif c.connector_type in OAUTH_CONNECTOR_TYPES:
            return _test_oauth_connector(c)
        else:
            return jsonify({"ok": False, "message": f"Unknown type: {c.connector_type}"}), 400
    except Exception as e:
        log.warning("Connector test failed id=%s: %s", connector_id, e)
        return jsonify({"ok": False, "message": str(e)}), 200


def _test_rest_api(c: Connector):
    from app.services.connector_auth import build_auth_headers

    cfg = json.loads(c.config or "{}")
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        return jsonify({"ok": False, "message": "No base_url configured"}), 200

    import httpx
    headers = build_auth_headers(cfg.get("auth_type", "none"), c.get_auth())

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


def _test_azure_devops(c: Connector):
    from app.services.connector_auth import build_auth_headers

    cfg = json.loads(c.config or "{}")
    org_url = (cfg.get("org_url") or "").rstrip("/")
    if not org_url:
        return jsonify({"ok": False, "message": "No org_url configured"}), 200

    auth = c.get_auth()
    if not auth.get("pat"):
        return jsonify({"ok": False, "message": "No personal access token configured"}), 200

    import httpx
    # PAT auth is HTTP Basic with an empty username — see build_auth_headers("basic", ...).
    headers = build_auth_headers("basic", {"username": "", "password": auth["pat"]})
    url = f"{org_url}/_apis/projects?api-version=7.1"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 401:
            return jsonify({"ok": False, "message": "Auth failed (401) — check the personal access token"})
        return jsonify({"ok": resp.status_code < 500, "message": f"HTTP {resp.status_code}"})
    except httpx.ConnectError as e:
        return jsonify({"ok": False, "message": f"Connection refused: {e}"})
    except httpx.TimeoutException:
        return jsonify({"ok": False, "message": "Connection timed out (10s)"})


def _test_jira(c: Connector):
    from app.services.connector_auth import build_auth_headers

    cfg = json.loads(c.config or "{}")
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        return jsonify({"ok": False, "message": "No base_url configured"}), 200

    auth = c.get_auth()
    if not auth.get("email") or not auth.get("api_token"):
        return jsonify({"ok": False, "message": "No email/api_token configured"}), 200

    import httpx
    # Jira Cloud auth: HTTP Basic with email:api_token — not a bearer token.
    headers = build_auth_headers("basic", {"username": auth["email"], "password": auth["api_token"]})
    headers["Accept"] = "application/json"
    url = f"{base_url}/rest/api/3/myself"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 401:
            return jsonify({"ok": False, "message": "Auth failed (401) — check email/API token"})
        if resp.status_code == 200:
            name = resp.json().get("displayName", "?")
            return jsonify({"ok": True, "message": f"Authenticated as {name}"})
        return jsonify({"ok": resp.status_code < 500, "message": f"HTTP {resp.status_code}"})
    except httpx.ConnectError as e:
        return jsonify({"ok": False, "message": f"Connection refused: {e}"})
    except httpx.TimeoutException:
        return jsonify({"ok": False, "message": "Connection timed out (10s)"})


def _test_linear(c: Connector):
    auth = c.get_auth()
    if not auth.get("api_key"):
        return jsonify({"ok": False, "message": "No API key configured"}), 200

    import httpx
    # Linear's API accepts the raw key as Authorization — no "Bearer " prefix.
    headers = {"Authorization": auth["api_key"], "Content-Type": "application/json"}
    query = {"query": "{ viewer { name } }"}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post("https://api.linear.app/graphql", headers=headers, json=query)
        if resp.status_code == 401:
            return jsonify({"ok": False, "message": "Auth failed (401) — check the API key"})
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errors"):
                return jsonify({"ok": False, "message": str(data["errors"])[:300]})
            name = (data.get("data") or {}).get("viewer", {}).get("name", "?")
            return jsonify({"ok": True, "message": f"Authenticated as {name}"})
        return jsonify({"ok": resp.status_code < 500, "message": f"HTTP {resp.status_code}"})
    except httpx.ConnectError as e:
        return jsonify({"ok": False, "message": f"Connection refused: {e}"})
    except httpx.TimeoutException:
        return jsonify({"ok": False, "message": "Connection timed out (10s)"})


def _test_oauth_connector(c: Connector):
    from app.services import oauth

    cfg = json.loads(c.config or "{}")
    auth = c.get_auth()
    if not auth.get("client_id") or not auth.get("client_secret"):
        return jsonify({"ok": False, "message": "No client_id/client_secret configured"}), 200
    if not auth.get("refresh_token"):
        return jsonify({"ok": False, "message": "Not connected yet — click Connect to complete OAuth consent"}), 200

    provider_cfg = get_provider_config(c.connector_type, cfg)
    try:
        oauth.get_valid_access_token(c, provider_cfg["token_endpoint"])
        return jsonify({"ok": True, "message": "Connected — access token is valid"})
    except oauth.ReAuthRequired as e:
        return jsonify({"ok": False, "message": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Token refresh failed: {e}"})


# ── OAuth connect flow ───────────────────────────────────────────────────────

@bp.route("/api/connectors/<connector_id>/oauth/start", methods=["POST"])
def start_connector_oauth(connector_id):
    c = Connector.query.get(connector_id)
    if not c:
        return jsonify({"error": "Connector not found"}), 404
    if c.connector_type not in OAUTH_CONNECTOR_TYPES:
        return jsonify({"error": f"{c.connector_type} is not an OAuth connector"}), 400

    cfg = json.loads(c.config or "{}")
    auth = c.get_auth()
    client_id = auth.get("client_id")
    client_secret = auth.get("client_secret")
    if not client_id or not client_secret:
        return jsonify({"error": "client_id and client_secret must be configured before connecting"}), 400

    from app.services import oauth
    provider_cfg = get_provider_config(c.connector_type, cfg)
    try:
        result = oauth.start_oauth_flow(
            c.id, provider_cfg["authorize_endpoint"], provider_cfg["token_endpoint"],
            client_id, client_secret, provider_cfg["scope"],
            provider_cfg.get("extra_authorize_params"),
        )
    except Exception as e:
        log.warning("OAuth start failed for connector %s: %s", connector_id, e)
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


@bp.route("/api/connectors/<connector_id>/oauth/status", methods=["GET"])
def connector_oauth_status(connector_id):
    c = Connector.query.get(connector_id)
    if not c:
        return jsonify({"error": "Connector not found"}), 404
    flow_id = request.args.get("flow_id")
    if not flow_id:
        return jsonify({"error": "flow_id is required"}), 400
    from app.services import oauth
    return jsonify(oauth.get_oauth_flow_status(flow_id))


def _test_github(c: Connector):
    from app.services.connector_auth import build_auth_headers

    auth = c.get_auth()
    if not auth.get("pat"):
        return jsonify({"ok": False, "message": "No personal access token configured"}), 200

    import httpx
    headers = build_auth_headers("bearer", {"token": auth["pat"]})
    headers["Accept"] = "application/vnd.github+json"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get("https://api.github.com/user", headers=headers)
        if resp.status_code == 401:
            return jsonify({"ok": False, "message": "Auth failed (401) — check the personal access token"})
        if resp.status_code == 200:
            login = resp.json().get("login", "?")
            return jsonify({"ok": True, "message": f"Authenticated as {login}"})
        return jsonify({"ok": resp.status_code < 500, "message": f"HTTP {resp.status_code}"})
    except httpx.ConnectError as e:
        return jsonify({"ok": False, "message": f"Connection refused: {e}"})
    except httpx.TimeoutException:
        return jsonify({"ok": False, "message": "Connection timed out (10s)"})
