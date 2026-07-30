"""
Shared connector helpers: auth-header construction and outbound-request safety
checks, used by both the connector CRUD/test routes and the MCP call_connector
tool so the two don't drift (this module has no Flask route dependencies).
"""
from __future__ import annotations

import base64
import ipaddress
import socket
from urllib.parse import urlparse


def build_auth_headers(auth_type: str, auth: dict | None) -> dict:
    """Build request headers for a connector's configured auth_type."""
    auth = auth or {}
    headers: dict = {}
    if auth_type == "bearer" and auth.get("token"):
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif auth_type == "api_key" and auth.get("api_key"):
        headers[auth.get("header_name") or "X-API-Key"] = auth["api_key"]
    elif auth_type == "basic" and auth.get("username"):
        creds = base64.b64encode(f"{auth['username']}:{auth.get('password', '')}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    return headers


def validate_action_segment(action: str) -> str | None:
    """Return an error string if `action` is unsafe to append as a URL path
    segment (e.g. an LLM-controlled value trying to redirect the request to a
    different host or escape the connector's configured base path), else None.
    """
    if not action or not isinstance(action, str):
        return "Error: action is required"
    if "://" in action or action.startswith("//"):
        return "Error: action must not contain a URL scheme or host"
    if ".." in action:
        return "Error: action must not contain '..'"
    return None


# Hosts an agent-controlled connector call must never be able to reach: this
# app's own API (which has no auth and normally only listens on loopback) and
# link-local/metadata endpoints. Ordinary private (RFC1918) addresses are
# intentionally allowed — this app's target use case includes calling
# on-prem/corporate REST APIs that live on a private LAN.
def is_blocked_host(host: str) -> bool:
    """True if `host` is (or resolves to) a loopback, link-local, multicast, or
    otherwise reserved address that a connector call should never target."""
    if not host:
        return True
    lowered = host.strip().lower()
    if lowered in ("localhost", "localhost.localdomain"):
        return True

    # IP literal — check directly, no DNS involved.
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified
    except ValueError:
        pass

    # Hostname — best-effort DNS check. This app is offline-first, so a failed
    # lookup (no network, DNS hiccup) is not treated as blocked — the outbound
    # request will simply fail on its own if the host is unreachable. This is
    # a best-effort guard against the common case (an agent-controlled action
    # targeting a literal loopback/link-local address), not a defense against
    # a determined DNS-rebinding attacker.
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str.split("%")[0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return True
    return False


def validate_target_url(url: str) -> str | None:
    """Return an error string if `url`'s host is blocked, else None."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        return "Error: invalid URL"
    if not host:
        return "Error: invalid URL (no host)"
    if is_blocked_host(host):
        return f"Error: connector target '{host}' is not allowed (loopback/link-local addresses are blocked)"
    return None
