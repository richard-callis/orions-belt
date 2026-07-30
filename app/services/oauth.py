"""
Shared OAuth 2.0 core for desktop-app connectors (Google, Microsoft Graph,
Salesforce) — the RFC 8252 loopback-redirect + PKCE flow appropriate for a
native/desktop app (no client-secret-in-JS, no embedded-webview consent
screen, which every major provider blocks anyway).

NONE of this has been exercised against a live provider in this environment
— there is no way to complete an interactive OAuth consent flow here. The
code matches each provider's documented OAuth spec, but the loopback-
listener lifecycle, the token-exchange/refresh round trip, and (the single
most likely real-world failure per review) a provider silently omitting
refresh_token on re-consent have not been verified end-to-end. Whoever
first connects a Google/Graph/Salesforce connector should treat that as
the real test of this code, not a formality.

Flow:
  1. start_oauth_flow(provider_config) — generates a PKCE pair + state,
     starts a one-shot loopback HTTP listener on an OS-assigned port, opens
     the provider's consent URL in the system's default browser (never an
     embedded webview), and returns immediately with the flow's id.
  2. The user completes consent in their browser; the provider redirects to
     http://127.0.0.1:<port>/callback?code=...&state=...; the loopback
     listener captures it, exchanges the code for tokens, and stores them
     (encrypted) on the Connector row.
  3. get_oauth_flow_status(flow_id) — polled by the frontend to learn when
     step 2 completed (or failed).
  4. get_valid_access_token(connector) — used by connector-action tools;
     refreshes if the stored token is expired/near-expiry, persisting the
     refreshed tokens back to the connector.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import secrets
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, parse_qs

log = logging.getLogger("orions-belt.oauth")

# In-memory only — an in-progress consent flow does not need to survive an
# app restart, and storing a PKCE verifier at rest for longer than the few
# minutes a flow takes has no upside.
_flows: dict[str, dict] = {}
_flows_lock = threading.Lock()

_REFRESH_MARGIN = timedelta(minutes=5)
_CALLBACK_TIMEOUT_SECONDS = 300
# How long a finished flow (connected or error) stays in _flows after
# reaching that terminal state, before being dropped. Long enough that the
# frontend's status-polling loop has certainly observed the final state at
# least once; short enough to bound how long a flow's client_secret and PKCE
# code_verifier sit in plaintext in process memory once they're no longer
# needed for anything.
_FLOW_RETENTION_AFTER_TERMINAL_SECONDS = 300


def _schedule_flow_cleanup(flow_id: str, delay: float = _FLOW_RETENTION_AFTER_TERMINAL_SECONDS) -> None:
    def _cleanup():
        with _flows_lock:
            _flows.pop(flow_id, None)
    t = threading.Timer(delay, _cleanup)
    t.daemon = True
    t.start()


class ReAuthRequired(Exception):
    """Raised when a refresh attempt fails with invalid_grant — the stored
    refresh token is no longer usable and the user must reconnect from
    scratch. Never retried automatically (would just loop against a dead
    token)."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) per RFC 7636 (S256)."""
    verifier = _b64url(secrets.token_bytes(40))  # 43-128 chars required; this yields ~53
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles exactly one GET /callback, then the server that owns this
    handler is shut down by the caller."""

    def do_GET(self):  # noqa: N802 - required name by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        self.server.oauth_result = {
            "code": (params.get("code") or [None])[0],
            "state": (params.get("state") or [None])[0],
            "error": (params.get("error") or [None])[0],
            "error_description": (params.get("error_description") or [None])[0],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if self.server.oauth_result.get("error"):
            body = "<html><body><h3>Connection failed.</h3><p>You can close this tab.</p></body></html>"
        else:
            body = "<html><body><h3>Connected.</h3><p>You can close this tab and return to Orion's Belt.</p></body></html>"
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):  # silence default stderr logging
        pass


def _run_loopback_listener(app, flow_id: str):
    """Runs in a background thread: waits for the /callback request (or
    times out), then exchanges the code for tokens and updates the flow's
    status. Binds to 127.0.0.1 only — never 0.0.0.0 — a listener accepting
    the OAuth redirect must not be reachable from the network.

    `app` is the Flask app captured at start_oauth_flow()-call-time (via
    current_app._get_current_object()) — this thread has no app context of
    its own otherwise, and _store_tokens() needs one for its DB write."""
    with _flows_lock:
        flow = _flows.get(flow_id)
    if not flow:
        return

    server = http.server.HTTPServer(("127.0.0.1", flow["port"]), _CallbackHandler)
    server.oauth_result = None
    deadline = time.monotonic() + _CALLBACK_TIMEOUT_SECONDS
    try:
        # Keep serving requests until /callback actually hits (the handler
        # only sets oauth_result for that path — see _CallbackHandler) or
        # the overall deadline passes. A single handle_request() call is
        # satisfied by ANY request that reaches this port first — a favicon
        # fetch, a browser prefetch, a stray port probe — which would
        # otherwise drop a still-in-progress consent flow into "timed out"
        # while the user is still sitting on the provider's consent screen.
        while server.oauth_result is None and time.monotonic() < deadline:
            server.timeout = max(0.1, deadline - time.monotonic())
            server.handle_request()
    except Exception as e:
        log.warning("OAuth flow %s: loopback listener error: %s", flow_id, e)
    finally:
        server.server_close()

    result = getattr(server, "oauth_result", None)
    with _flows_lock:
        flow = _flows.get(flow_id)
        if not flow:
            return
        if not result:
            flow["status"] = "error"
            flow["error"] = "Timed out waiting for the browser redirect."
            _schedule_flow_cleanup(flow_id)
            return
        if result.get("error"):
            flow["status"] = "error"
            flow["error"] = result.get("error_description") or result["error"]
            _schedule_flow_cleanup(flow_id)
            return
        if result.get("state") != flow["state"]:
            flow["status"] = "error"
            flow["error"] = "State mismatch — possible CSRF, aborting."
            _schedule_flow_cleanup(flow_id)
            return
        if not result.get("code"):
            flow["status"] = "error"
            flow["error"] = "No authorization code in the callback."
            _schedule_flow_cleanup(flow_id)
            return
        flow["code"] = result["code"]
        flow["status"] = "exchanging"

    try:
        tokens = exchange_code_for_tokens(
            flow["token_endpoint"], flow["client_id"], flow["client_secret"],
            flow["redirect_uri"], result["code"], flow["code_verifier"],
        )
        with app.app_context():
            _store_tokens(flow["connector_id"], tokens)
        with _flows_lock:
            _flows[flow_id]["status"] = "connected"
    except Exception as e:
        log.warning("OAuth flow %s: token exchange failed: %s", flow_id, e)
        with _flows_lock:
            _flows[flow_id]["status"] = "error"
            _flows[flow_id]["error"] = str(e)
    finally:
        _schedule_flow_cleanup(flow_id)


def start_oauth_flow(connector_id: str, authorize_endpoint: str, token_endpoint: str,
                      client_id: str, client_secret: str, scope: str,
                      extra_authorize_params: dict | None = None) -> dict:
    """Start a consent flow for `connector_id`. Opens the system browser.
    Returns {flow_id, authorize_url} — poll get_oauth_flow_status(flow_id)."""
    import socketserver
    with socketserver.TCPServer(("127.0.0.1", 0), None) as tmp:
        port = tmp.server_address[1]

    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    flow_id = str(uuid.uuid4())

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if extra_authorize_params:
        params.update(extra_authorize_params)
    authorize_url = f"{authorize_endpoint}?{urlencode(params)}"

    with _flows_lock:
        _flows[flow_id] = {
            "connector_id": connector_id, "status": "waiting", "error": None,
            "port": port, "state": state, "code_verifier": verifier,
            "redirect_uri": redirect_uri, "token_endpoint": token_endpoint,
            "client_id": client_id, "client_secret": client_secret,
        }

    from flask import current_app
    app = current_app._get_current_object()
    threading.Thread(target=_run_loopback_listener, args=(app, flow_id), daemon=True).start()
    webbrowser.open(authorize_url)

    return {"flow_id": flow_id, "authorize_url": authorize_url}


def get_oauth_flow_status(flow_id: str) -> dict:
    with _flows_lock:
        flow = _flows.get(flow_id)
    if not flow:
        return {"status": "unknown"}
    return {"status": flow["status"], "error": flow.get("error")}


def exchange_code_for_tokens(token_endpoint: str, client_id: str, client_secret: str,
                             redirect_uri: str, code: str, code_verifier: str) -> dict:
    """POST the authorization_code grant. Raises if the response has no
    refresh_token — the single most likely real-world failure for an
    unattended flow like this (a provider omits it on re-consent when the
    user already granted access previously), and one that's silent for
    ~an hour (until the access token expires) if not caught immediately."""
    import httpx
    resp = httpx.post(token_endpoint, data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": code_verifier,
    }, timeout=30.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed: HTTP {resp.status_code}: {resp.text[:500]}")
    tokens = resp.json()
    if not tokens.get("refresh_token"):
        raise RuntimeError(
            "The provider did not return a refresh token. This usually means you've "
            "already granted this app access before and the provider silently skipped "
            "issuing a new one. Revoke this app's access in the provider's account "
            "settings, then reconnect."
        )
    return tokens


def refresh_access_token(token_endpoint: str, client_id: str, client_secret: str,
                         refresh_token: str) -> dict:
    import httpx
    resp = httpx.post(token_endpoint, data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }, timeout=30.0)
    if resp.status_code != 200:
        body = resp.text[:500]
        if "invalid_grant" in body:
            raise ReAuthRequired("Refresh token is no longer valid — reconnect this connector.")
        raise RuntimeError(f"Token refresh failed: HTTP {resp.status_code}: {body}")
    tokens = resp.json()
    # Some providers omit refresh_token on a refresh response (the original
    # one stays valid) — preserve it rather than losing it.
    if not tokens.get("refresh_token"):
        tokens["refresh_token"] = refresh_token
    return tokens


def _store_tokens(connector_id: str, tokens: dict) -> None:
    from app import db
    from app.models.connector import Connector

    connector = Connector.query.get(connector_id)
    if not connector:
        log.warning("OAuth: connector %s no longer exists, dropping tokens", connector_id)
        return
    expires_in = tokens.get("expires_in", 3600)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
    auth = connector.get_auth()
    auth.update({
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": expires_at,
        "token_type": tokens.get("token_type", "Bearer"),
    })
    if tokens.get("instance_url"):
        # Salesforce returns the org's actual API host here — the connector's
        # configured instance_url may be nothing more than the generic
        # login.salesforce.com the user authenticated against, which is not
        # a valid API host for REST calls after login.
        auth["instance_url"] = tokens["instance_url"]
    connector.set_auth(auth)
    db.session.commit()


_connector_locks: dict[str, threading.Lock] = {}
_connector_locks_meta_lock = threading.Lock()


def _lock_for_connector(connector_id: str) -> threading.Lock:
    with _connector_locks_meta_lock:
        lock = _connector_locks.get(connector_id)
        if lock is None:
            lock = threading.Lock()
            _connector_locks[connector_id] = lock
        return lock


def _needs_refresh(auth: dict) -> bool:
    access_token = auth.get("access_token")
    expires_at_raw = auth.get("expires_at")
    if not (access_token and expires_at_raw):
        return True
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= (expires_at - _REFRESH_MARGIN)


def get_valid_access_token(connector, token_endpoint: str) -> str:
    """Return a usable access token for `connector`, refreshing first if
    it's expired or within 5 minutes of expiring. Persists a refreshed
    token back to the connector. Raises ReAuthRequired if the refresh token
    itself is no longer valid."""
    from app import db

    auth = connector.get_auth()
    refresh_token = auth.get("refresh_token")
    if not refresh_token:
        raise ReAuthRequired("Connector has never completed the OAuth consent flow.")

    if not _needs_refresh(auth):
        return auth["access_token"]

    # Two concurrent callers (e.g. two rooms using the same connector) can
    # both observe "needs refresh" at once. Several providers (Salesforce,
    # Graph) rotate the refresh token on every use, so letting both actually
    # call refresh_access_token unsynchronized means the loser persists (or
    # the provider rejects) a refresh_token that's already been superseded —
    # breaking the connector until a full manual reconnect.
    with _lock_for_connector(connector.id):
        # Re-read after acquiring the lock: another thread may have already
        # refreshed and committed while this one was waiting, in which case
        # this thread's in-memory `auth` (read before the lock) is stale.
        db.session.refresh(connector)
        auth = connector.get_auth()
        refresh_token = auth.get("refresh_token")
        if not refresh_token:
            raise ReAuthRequired("Connector has never completed the OAuth consent flow.")
        if not _needs_refresh(auth):
            return auth["access_token"]

        tokens = refresh_access_token(
            token_endpoint, auth.get("client_id", ""), auth.get("client_secret", ""), refresh_token,
        )
        auth.update({
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))).isoformat(),
        })
        if tokens.get("instance_url"):
            auth["instance_url"] = tokens["instance_url"]
        connector.set_auth(auth)
        db.session.commit()
        return tokens["access_token"]
