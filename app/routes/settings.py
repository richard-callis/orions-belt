"""
Orion's Belt — Settings API
GET/PUT settings values, test LLM connection.
"""
import json
import time

import httpx

from flask import Blueprint, jsonify, request, render_template
from app import db
from app.models.settings import Setting

bp = Blueprint("settings", __name__)


@bp.route("/settings")
@bp.route("/settings/")
def index():
    return render_template("settings.html")


@bp.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "Orion's Belt", "version": "0.1.0"})


# ── Settings CRUD ─────────────────────────────────────────────────────────────

@bp.route("/api/settings/<key>", methods=["GET"])
def get_setting(key):
    """Get a single setting value."""
    row = Setting.query.get(key)
    value = None
    if row:
        if row.value_type == "json":
            value = json.loads(row.value) if row.value else None
        elif row.value_type == "bool":
            value = row.value == "true"
        elif row.value_type == "int":
            value = int(row.value) if row.value else None
        else:
            value = row.value
    return jsonify({"data": {"key": key, "value": value}})


@bp.route("/api/settings", methods=["GET"])
def list_settings():
    """List all settings."""
    rows = Setting.query.all()
    return jsonify([
        {"key": r.key, "value": r.value, "value_type": r.value_type}
        for r in rows
    ])


@bp.route("/api/settings/<key>", methods=["PUT"])
def set_setting(key):
    """Set a single setting value."""
    body = request.get_json() or {}
    value = body.get("value", "")

    Setting.set(key, value, value_type="string")
    return jsonify({"success": True, "key": key, "value": value})


@bp.route("/api/settings", methods=["POST"])
def set_settings_bulk():
    """Set multiple settings at once."""
    body = request.get_json() or {}
    results = []

    for key, value in body.items():
        Setting.set(key, value, value_type="string")
        results.append({"key": key, "value": value})

    return jsonify({"success": True, "settings": results})


# ── LLM Connection Test ───────────────────────────────────────────────────────

@bp.route("/api/llm/test", methods=["POST"])
def test_llm_connection():
    """Test connection to an LLM endpoint."""
    body = request.get_json() or {}
    base_url = body.get("base_url", "").strip()
    model = body.get("model", "").strip()

    if not base_url or not model:
        return jsonify({"error": "Base URL and model are required"}), 400

    start = time.time()

    try:
        # Try a minimal chat completion to test connectivity
        url = f"{base_url.rstrip('/')}/chat/completions"
        res = httpx.post(
            url,
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 5,
                "stream": False,
            },
            timeout=10.0,
        )

        latency_ms = int((time.time() - start) * 1000)

        if res.status_code == 200:
            data = res.json()
            resp_model = data.get("model", model)
            return jsonify({
                "success": True,
                "model": resp_model,
                "latency_ms": latency_ms,
            })
        elif res.status_code == 401 or res.status_code == 403:
            return jsonify({"error": f"Auth failed ({res.status_code}). Check your API key."}), 401
        else:
            error_text = res.text[:200]
            return jsonify({
                "error": f"HTTP {res.status_code}: {error_text}"
            }), res.status_code

    except httpx.TimeoutException:
        return jsonify({"error": f"Request timed out ({base_url})"}), 504
    except httpx.ConnectError as e:
        return jsonify({"error": f"Connection refused: {base_url}. Is the server running?"}), 503
    except httpx.ConnectTimeout:
        return jsonify({"error": f"Connection timed out: {base_url}"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500
