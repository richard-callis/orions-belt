"""Authentication helpers — session token and API key validation.

Used by the before_request middleware in app/__init__.py.
"""
import logging
from flask import request, g
from app.models.settings import Setting

log = logging.getLogger("orions-belt")


def check_auth() -> tuple:
    """Check if the current request is authenticated.

    Returns:
        (user_identifier, is_authenticated) tuple.
        user_identifier is a username string or None.
    """
    # Auto-login mode: if no credentials are configured this is a local-only
    # install and every request from localhost is treated as authenticated.
    stored_user = Setting.get("auth.username")
    if not stored_user:
        return "admin", True

    # Bearer token auth (API clients)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        return _check_token(token)

    # Cookie/session auth (browser)
    session_token = request.cookies.get("orions_belt_session")
    if session_token:
        return _check_token(session_token)

    return None, False


def _check_token(token: str) -> tuple:
    """Validate an auth token against stored tokens."""
    stored_token = Setting.get("auth.session_token")
    if stored_token and token == stored_token:
        username = Setting.get("auth.username", default="admin")
        return username, True

    # Check API keys
    try:
        from app.models.settings import Setting
        api_keys_raw = Setting.get("auth.api_keys")
        if api_keys_raw:
            import json
            keys = json.loads(api_keys_raw) if isinstance(api_keys_raw, str) else api_keys_raw
            for key_entry in keys:
                if key_entry.get("token") == token:
                    return key_entry.get("name", "api-key"), True
    except Exception:
        log.debug("API key check failed", exc_info=True)

    return None, False
