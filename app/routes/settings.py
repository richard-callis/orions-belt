"""
Orion's Belt — Settings API
GET/PUT settings values, test LLM connection, LLM provider management.
"""
import json
import logging
import time
import uuid

import httpx

from flask import Blueprint, jsonify, request, render_template
from app import db
from app.models.settings import Setting

bp = Blueprint("settings", __name__)
log = logging.getLogger("orions-belt")


@bp.route("/settings")
@bp.route("/settings/")
def index():
    return render_template("settings.html")


@bp.route("/api/pii/status")
def pii_status():
    """Quick check: is PII Guard operational? Returns within ~100ms."""
    try:
        from app.services.pii_guard import get_pii_guard
        guard = get_pii_guard()
        # Force initialization so we get accurate stage flags
        guard._ensure_initialized()
        available = guard._presidio_ready or guard._regex_ready or guard._ner_ready
        stages = {
            "presidio": guard._presidio_ready,
            "regex": guard._regex_ready,
            "ner": guard._ner_ready,
            "judge": guard._judge_ready,
        }
        fix_hint = None
        if not guard._presidio_ready and not guard._ner_ready:
            fix_hint = "pip install torch --index-url https://download.pytorch.org/whl/cpu"
        return jsonify({"available": available, "stages": stages, "fix_hint": fix_hint})
    except Exception as e:
        return jsonify({"available": False, "stages": {}, "error": str(e)})


@bp.route("/api/health")
def health():
    """Health check — returns component status without exposing secrets."""
    from app import db
    from app.models.settings import Setting as SettingModel

    # Check DB connectivity
    try:
        db.session.execute(db.text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "error"

    # Check LLM provider config
    providers = _get_providers()
    active = _get_active_provider()
    llm_status = "configured" if (active and active.get("base_url")) else "not_configured"

    return jsonify({
        "status": "healthy" if db_status == "connected" else "degraded",
        "app": "Orion's Belt",
        "version": "0.1.0",
        "database": db_status,
        "llm_provider": llm_status,
        "provider_count": len(providers),
    })


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
    row = Setting.query.get("llm.providers")
    if not row or not row.value:
        return _DEFAULT_PROVIDERS
    try:
        parsed = json.loads(row.value)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return _DEFAULT_PROVIDERS


def _get_active_provider_id():
    row = Setting.query.get("llm.active_provider")
    return row.value if row else None


def _get_active_provider():
    """Return the config dict for the active provider, or None."""
    providers = _get_providers()
    active_id = _get_active_provider_id()

    # If no active set or invalid, use the first provider
    if not active_id or not any(p.get("id") == active_id for p in providers):
        active_id = providers[0]["id"] if providers else None

    return next((p for p in providers if p.get("id") == active_id), None)


# ── Settings CRUD ─────────────────────────────────────────────────────────────

@bp.route("/api/settings/<key>", methods=["GET"])
def get_setting(key):
    """Get a single setting value. llm.providers API keys are masked."""
    row = Setting.query.get(key)
    value = None
    if row:
        if row.value_type == "json":
            parsed = json.loads(row.value) if row.value else None
            # SECURITY: mask API keys in provider list
            if key == "llm.providers" and isinstance(parsed, list):
                value = _redact_providers(parsed)
            else:
                value = parsed
        elif row.value_type == "bool":
            value = row.value == "true"
        elif row.value_type == "int":
            value = int(row.value) if row.value else None
        else:
            value = row.value
    return jsonify({"data": {"key": key, "value": value}})


@bp.route("/api/settings", methods=["GET"])
def list_settings():
    """List all settings. llm.providers API keys are masked."""
    rows = Setting.query.all()
    result = []
    for r in rows:
        if r.key == "llm.providers" and r.value:
            try:
                parsed = json.loads(r.value)
                if isinstance(parsed, list):
                    # SECURITY: mask API keys before returning to browser
                    result.append({
                        "key": r.key,
                        "value": _redact_providers(parsed),
                        "value_type": r.value_type,
                    })
                    continue
            except Exception:
                pass
        result.append({"key": r.key, "value": r.value, "value_type": r.value_type})
    return jsonify(result)


@bp.route("/api/settings/<key>", methods=["PUT"])
def set_setting(key):
    """Set a single setting value."""
    body = request.get_json()
    # Support both { "value": ... } and direct value
    if isinstance(body, dict):
        value = body.get("value", "")
    elif isinstance(body, list):
        value = body
    else:
        value = body or ""

    # Store certain keys as their proper types
    _JSON_KEYS = {"llm.providers"}
    _BOOL_KEYS = {"debug.llm", "pii.guard.enabled"}
    if key in _JSON_KEYS:
        Setting.set(key, value, value_type="json")
    elif key in _BOOL_KEYS:
        Setting.set(key, bool(value) if not isinstance(value, bool) else value, value_type="bool")
    else:
        Setting.set(key, value, value_type="string")

    return jsonify({"success": True, "key": key, "value": value})


@bp.route("/api/settings", methods=["POST"])
def set_settings_bulk():
    """Set multiple settings at once."""
    body = request.get_json() or {}
    results = []

    if not isinstance(body, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    for key, value in body.items():
        if key == "llm.providers":
            Setting.set(key, value, value_type="json")
        else:
            Setting.set(key, value, value_type="string")
        results.append({"key": key, "value": value})

    return jsonify({"success": True, "settings": results})


# ── LLM Provider endpoints ────────────────────────────────────────────────────

def _redact_providers(providers: list) -> list:
    """Return a copy of the provider list with API keys masked.

    SECURITY: API keys must never be returned to the browser in plaintext.
    The backend reads keys directly from DB when making LLM calls.
    """
    result = []
    for p in providers:
        raw_key = p.get("api_key", "")
        if raw_key and len(raw_key) > 4:
            masked = "*" * (len(raw_key) - 4) + raw_key[-4:]
        elif raw_key:
            masked = "*" * len(raw_key)
        else:
            masked = ""
        result.append({**p, "api_key": masked})
    return result


@bp.route("/api/prompts", methods=["GET"])
def get_prompts():
    """Return current system prompts (base + planning suffix) and their defaults."""
    from app.routes.chat import _BUILTIN_BASE_PROMPT, _BUILTIN_PLANNING_SUFFIX
    base = Setting.get("system_prompt.base") or ""
    planning = Setting.get("system_prompt.planning_suffix") or ""
    return jsonify({
        "base": base,
        "planning_suffix": planning,
        "default_base": _BUILTIN_BASE_PROMPT,
        "default_planning_suffix": _BUILTIN_PLANNING_SUFFIX,
    })


@bp.route("/api/prompts", methods=["PUT"])
def save_prompts():
    """Save system prompt settings."""
    body = request.get_json() or {}
    if "base" in body:
        val = body["base"].strip()
        Setting.set("system_prompt.base", val if val else None)
    if "planning_suffix" in body:
        val = body["planning_suffix"].strip()
        Setting.set("system_prompt.planning_suffix", val if val else None)
    return jsonify({"success": True})


@bp.route("/api/prompts/reset", methods=["POST"])
def reset_prompts():
    """Reset system prompts to built-in defaults (deletes the setting rows)."""
    for key in ("system_prompt.base", "system_prompt.planning_suffix"):
        row = Setting.query.get(key)
        if row:
            db.session.delete(row)
    db.session.commit()
    return jsonify({"success": True})


@bp.route("/api/llm/debug", methods=["GET"])
def debug_providers():
    """Debug: show raw and parsed provider data. API keys are redacted."""
    providers = _get_providers()
    active_id = _get_active_provider_id()
    row = Setting.query.get("llm.providers")
    row_type = row.value_type if row else None
    return jsonify({
        "providers": _redact_providers(providers),
        "active_id": active_id,
        "row_value_type": row_type,
    })


@bp.route("/api/llm/providers", methods=["GET"])
def get_llm_providers():
    """Get list of LLM providers with active indicator. API keys are masked."""
    providers = _get_providers()
    active_id = _get_active_provider_id()
    # SECURITY: mask API keys — the frontend only needs to know a key is set,
    # not the key itself. The backend reads the raw key from DB at call time.
    return jsonify({
        "providers": _redact_providers(providers),
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
    log.info("Provider added: name=%r id=%s key_set=%s", name, new_provider["id"], bool(api_key))

    # Auto-select as active if it's the first provider
    if len(providers) == 1:
        Setting.set("llm.active_provider", new_provider["id"])

    return jsonify({"success": True, "provider": new_provider}), 201


@bp.route("/api/llm/providers/<provider_id>", methods=["PATCH"])
def update_llm_provider(provider_id):
    """Update an existing LLM provider. Preserves api_key if the submitted
    value is blank or a masked placeholder (starts with '*')."""
    body = request.get_json() or {}
    providers = _get_providers()
    idx = next((i for i, p in enumerate(providers) if p.get("id") == provider_id), None)
    if idx is None:
        return jsonify({"error": "Provider not found"}), 404

    for field in ("name", "type", "base_url", "model"):
        if field in body:
            providers[idx][field] = body[field]

    # Only overwrite the key if the user typed an actual new one.
    # Masked values (from the GET endpoint redaction) are ignored.
    new_key = body.get("api_key", "")
    key_updated = False
    if new_key and not new_key.startswith("*"):
        providers[idx]["api_key"] = new_key
        key_updated = True

    Setting.set("llm.providers", providers, value_type="json")
    log.info("Provider updated: id=%s key_updated=%s key_set=%s",
             provider_id, key_updated, bool(providers[idx].get("api_key")))
    return jsonify({"success": True, "provider": _redact_providers([providers[idx]])[0]})


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
    """Get the config for the active LLM provider. API key is masked."""
    provider = _get_active_provider()
    if not provider:
        return jsonify({"error": "No active provider configured"}), 404

    # SECURITY: never return plaintext API key to the browser
    raw_key = provider.get("api_key", "")
    masked = ("*" * (len(raw_key) - 4) + raw_key[-4:]) if len(raw_key) > 4 else ("*" * len(raw_key))
    return jsonify({
        "base_url": provider.get("base_url", ""),
        "api_key": masked,
        "model": provider.get("model", ""),
        "type": provider.get("type", "genai"),
        "id": provider.get("id", ""),
    })


# ── LLM Connection Test ───────────────────────────────────────────────────────

@bp.route("/api/llm/test", methods=["POST"])
def test_llm_connection():
    """Test connection to an LLM endpoint.

    Accepts optional fields:
      api_key      — key typed in the form (takes priority)
      provider_id  — fall back to the stored key for this provider if api_key blank/masked
    """
    import logging
    log = logging.getLogger("orions-belt")

    body = request.get_json() or {}
    base_url = body.get("base_url", "").strip()
    model = body.get("model", "").strip()

    if not base_url or not model:
        return jsonify({"error": "Base URL and model are required"}), 400

    # Resolve API key: form value > stored provider key > empty
    raw_key = body.get("api_key", "")
    if not raw_key or raw_key.startswith("*"):
        # masked or blank — look up the real key from the DB
        provider_id = body.get("provider_id", "")
        if provider_id:
            stored = next((p for p in _get_providers() if p.get("id") == provider_id), None)
            raw_key = (stored or {}).get("api_key", "")

    masked_key = ("*" * (len(raw_key) - 4) + raw_key[-4:]) if len(raw_key) > 4 else ("*" * len(raw_key)) if raw_key else "(none)"
    log.info("LLM test: url=%s model=%s key_set=%s", base_url, model, bool(raw_key))

    start = time.time()

    try:
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if raw_key:
            headers["Authorization"] = f"Bearer {raw_key}"

        log.info(
            "llm.test.request  POST %s  model=%s  auth=Bearer %s",
            url, model, masked_key,
        )

        res = httpx.post(
            url,
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 5,
                "stream": False,
            },
            headers=headers,
            timeout=10.0,
        )

        latency_ms = int((time.time() - start) * 1000)

        log.info("llm.test.response  status=%d url=%s", res.status_code, url)
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
