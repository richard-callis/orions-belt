"""
Orion's Belt — Settings API
GET/PUT settings values, test LLM connection, LLM provider management.
"""
import json
import time
import uuid

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


# ── LLM Provider helpers ──────────────────────────────────────────────────────

_DEFAULT_PROVIDERS = [
    {
        "id": "default",
        "name": "Default (OpenAI)",
        "type": "genai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o",
    }
]


def _get_providers():
    raw = Setting.get("llm.providers")
    if not raw:
        return _DEFAULT_PROVIDERS
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return _DEFAULT_PROVIDERS
    return raw


def _get_active_provider_id():
    return Setting.get("llm.active_provider")


def _get_active_provider():
    """Return the config dict for the active provider, or None."""
    providers = _get_providers()
    active_id = _get_active_provider_id()

    # If no active set, use the first provider
    if not active_id or not any(p.get("id") == active_id for p in providers):
        active_id = providers[0]["id"] if providers else None

    return next((p for p in providers if p.get("id") == active_id), None)


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

    # llm.providers is stored as JSON
    if key == "llm.providers":
        Setting.set(key, value, value_type="json")
    else:
        Setting.set(key, value, value_type="string")

    return jsonify({"success": True, "key": key, "value": value})


@bp.route("/api/settings", methods=["POST"])
def set_settings_bulk():
    """Set multiple settings at once."""
    body = request.get_json() or {}
    results = []

    for key, value in body.items():
        if key == "llm.providers":
            Setting.set(key, value, value_type="json")
        else:
            Setting.set(key, value, value_type="string")
        results.append({"key": key, "value": value})

    return jsonify({"success": True, "settings": results})


# ── LLM Provider endpoints ────────────────────────────────────────────────────

@bp.route("/api/llm/providers", methods=["GET"])
def get_llm_providers():
    """Get list of LLM providers with active indicator."""
    providers = _get_providers()
    active_id = _get_active_provider_id()
    return jsonify({
        "providers": providers,
        "active_id": active_id,
    })


@bp.route("/api/llm/providers", methods=["POST"])
def add_llm_provider():
    """Add a new LLM provider."""
    body = request.get_json() or {}
    name = body.get("name", "").strip()
    provider_type = body.get("type", "genai")
    base_url = body.get("base_url", "").strip()
    api_key = body.get("api_key", "")
    model = body.get("model", "").strip()

    if not name or not base_url or not model:
        return jsonify({"error": "Name, Base URL, and Model are required"}), 400

    providers = _get_providers()
    new_provider = {
        "id": str(uuid.uuid4()),
        "name": name,
        "type": provider_type,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
    }
    providers.append(new_provider)
    Setting.set("llm.providers", providers, value_type="json")

    # Auto-select as active if it's the first provider
    if len(providers) == 1:
        Setting.set("llm.active_provider", new_provider["id"])

    return jsonify({"success": True, "provider": new_provider}), 201


@bp.route("/api/llm/providers/<provider_id>", methods=["DELETE"])
def delete_llm_provider(provider_id):
    """Delete an LLM provider."""
    providers = _get_providers()
    providers = [p for p in providers if p.get("id") != provider_id]

    # If we deleted the active one, set a new active
    active_id = _get_active_provider_id()
    if active_id == provider_id:
        new_active = providers[0]["id"] if providers else None
        Setting.set("llm.active_provider", new_active)

    Setting.set("llm.providers", providers, value_type="json")
    return jsonify({"success": True})


@bp.route("/api/llm/providers/<provider_id>/activate", methods=["PUT"])
def activate_llm_provider(provider_id):
    """Set a provider as active."""
    providers = _get_providers()
    if not any(p.get("id") == provider_id for p in providers):
        return jsonify({"error": "Provider not found"}), 404

    Setting.set("llm.active_provider", provider_id)
    return jsonify({"success": True, "active_id": provider_id})


@bp.route("/api/llm/config", methods=["GET"])
def get_llm_config():
    """Get the config for the active LLM provider."""
    provider = _get_active_provider()
    if not provider:
        return jsonify({"error": "No active provider configured"}), 404

    return jsonify({
        "base_url": provider.get("base_url", ""),
        "api_key": provider.get("api_key", ""),
        "model": provider.get("model", ""),
        "type": provider.get("type", "genai"),
        "id": provider.get("id", ""),
    })


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
        elif res.status_code in (401, 403):
            return jsonify({
                "error": f"Auth failed ({res.status_code}). Check your API key."
            }), 401
        else:
            error_text = res.text[:200]
            return jsonify({
                "error": f"HTTP {res.status_code}: {error_text}"
            }), res.status_code

    except httpx.TimeoutException:
        return jsonify({"error": f"Request timed out ({base_url})"}), 504
    except httpx.ConnectError as e:
        return jsonify({
            "error": f"Connection refused: {base_url}. Is the server running?"
        }), 503
    except httpx.ConnectTimeout:
        return jsonify({"error": f"Connection timed out: {base_url}"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500
