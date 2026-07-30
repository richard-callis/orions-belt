"""
Tests for the shared OAuth core (app/services/oauth.py) — PKCE generation,
the real loopback HTTP listener (a genuine local socket, no live provider
needed), token exchange/refresh, and get_valid_access_token's refresh
decision logic. The one thing NOT testable here is the actual browser-based
consent step against a real provider.
"""
import time
import urllib.request

import pytest

from app import db
from app.models.connector import Connector
import app.services.oauth as oauth_mod


class TestPkce:
    def test_verifier_and_challenge_differ(self):
        verifier, challenge = oauth_mod.generate_pkce_pair()
        assert verifier != challenge

    def test_verifier_length_within_rfc7636_bounds(self):
        verifier, _ = oauth_mod.generate_pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_challenge_is_deterministic_sha256_of_verifier(self):
        import hashlib
        verifier, challenge = oauth_mod.generate_pkce_pair()
        expected = oauth_mod._b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        assert challenge == expected

    def test_successive_pairs_are_unique(self):
        pairs = {oauth_mod.generate_pkce_pair()[0] for _ in range(10)}
        assert len(pairs) == 10


class TestExchangeCodeForTokens:
    def test_raises_when_refresh_token_missing(self, monkeypatch):
        class FakeResp:
            status_code = 200
            def json(self):
                return {"access_token": "abc", "expires_in": 3600}  # no refresh_token
            text = ""

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        with pytest.raises(RuntimeError, match="refresh token"):
            oauth_mod.exchange_code_for_tokens("https://x/token", "cid", "secret", "http://x", "code", "verifier")

    def test_returns_tokens_when_refresh_token_present(self, monkeypatch):
        class FakeResp:
            status_code = 200
            def json(self):
                return {"access_token": "abc", "refresh_token": "def", "expires_in": 3600}
            text = ""

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        tokens = oauth_mod.exchange_code_for_tokens("https://x/token", "cid", "secret", "http://x", "code", "verifier")
        assert tokens["access_token"] == "abc"
        assert tokens["refresh_token"] == "def"

    def test_raises_on_http_error(self, monkeypatch):
        class FakeResp:
            status_code = 400
            text = "invalid_request"

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        with pytest.raises(RuntimeError, match="Token exchange failed"):
            oauth_mod.exchange_code_for_tokens("https://x/token", "cid", "secret", "http://x", "code", "verifier")


class TestRefreshAccessToken:
    def test_refreshes_successfully(self, monkeypatch):
        class FakeResp:
            status_code = 200
            def json(self):
                return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
            text = ""

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        tokens = oauth_mod.refresh_access_token("https://x/token", "cid", "secret", "old-refresh")
        assert tokens["access_token"] == "new-access"

    def test_preserves_refresh_token_if_provider_omits_it(self, monkeypatch):
        class FakeResp:
            status_code = 200
            def json(self):
                return {"access_token": "new-access", "expires_in": 3600}  # no refresh_token
            text = ""

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        tokens = oauth_mod.refresh_access_token("https://x/token", "cid", "secret", "old-refresh")
        assert tokens["refresh_token"] == "old-refresh"

    def test_invalid_grant_raises_reauth_required(self, monkeypatch):
        class FakeResp:
            status_code = 400
            text = '{"error": "invalid_grant"}'

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        with pytest.raises(oauth_mod.ReAuthRequired):
            oauth_mod.refresh_access_token("https://x/token", "cid", "secret", "dead-refresh")

    def test_other_http_error_raises_runtime_error(self, monkeypatch):
        class FakeResp:
            status_code = 500
            text = "server error"

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        with pytest.raises(RuntimeError, match="Token refresh failed"):
            oauth_mod.refresh_access_token("https://x/token", "cid", "secret", "refresh")


class TestGetValidAccessToken:
    def _make_connector(self, cid, auth):
        c = Connector(id=cid, name=cid, connector_type="google")
        c.set_auth(auth)
        return c

    def test_returns_cached_token_when_not_near_expiry(self, app, monkeypatch):
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(oauth_mod, "refresh_access_token", lambda *a, **k: pytest.fail("should not refresh"))
        with app.app_context():
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            c = self._make_connector("oauth-c1", {
                "access_token": "still-good", "refresh_token": "r", "expires_at": future,
                "client_id": "cid", "client_secret": "secret",
            })
            db.session.add(c)
            db.session.commit()
            try:
                token = oauth_mod.get_valid_access_token(c, "https://x/token")
                assert token == "still-good"
            finally:
                Connector.query.filter_by(id="oauth-c1").delete()
                db.session.commit()

    def test_refreshes_when_near_expiry(self, app, monkeypatch):
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(oauth_mod, "refresh_access_token",
                            lambda *a, **k: {"access_token": "refreshed", "refresh_token": "r2", "expires_in": 3600})
        with app.app_context():
            soon = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
            c = self._make_connector("oauth-c2", {
                "access_token": "stale", "refresh_token": "r", "expires_at": soon,
                "client_id": "cid", "client_secret": "secret",
            })
            db.session.add(c)
            db.session.commit()
            try:
                token = oauth_mod.get_valid_access_token(c, "https://x/token")
                assert token == "refreshed"
                reloaded = Connector.query.get("oauth-c2")
                assert reloaded.get_auth()["access_token"] == "refreshed"
            finally:
                Connector.query.filter_by(id="oauth-c2").delete()
                db.session.commit()

    def test_refreshes_when_no_expires_at_recorded(self, app, monkeypatch):
        monkeypatch.setattr(oauth_mod, "refresh_access_token",
                            lambda *a, **k: {"access_token": "refreshed", "refresh_token": "r2", "expires_in": 3600})
        with app.app_context():
            c = self._make_connector("oauth-c3", {"access_token": "x", "refresh_token": "r"})
            db.session.add(c)
            db.session.commit()
            try:
                token = oauth_mod.get_valid_access_token(c, "https://x/token")
                assert token == "refreshed"
            finally:
                Connector.query.filter_by(id="oauth-c3").delete()
                db.session.commit()

    def test_raises_reauth_required_if_never_connected(self, app):
        with app.app_context():
            c = self._make_connector("oauth-c4", {"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()
            try:
                with pytest.raises(oauth_mod.ReAuthRequired):
                    oauth_mod.get_valid_access_token(c, "https://x/token")
            finally:
                Connector.query.filter_by(id="oauth-c4").delete()
                db.session.commit()


class TestLoopbackListenerRealSocket:
    """Exercises the actual local HTTP listener with a real socket — the one
    part of this flow fully testable without a live OAuth provider."""

    def test_callback_captures_code_and_state_then_exchanges_tokens(self, app, monkeypatch):
        with app.app_context():
            c = Connector(id="oauth-loop1", name="oauth-loop1", connector_type="google")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()

        captured_exchange = {}

        def fake_exchange(token_endpoint, client_id, client_secret, redirect_uri, code, verifier):
            captured_exchange.update(code=code, verifier=verifier)
            return {"access_token": "a", "refresh_token": "b", "expires_in": 3600}

        monkeypatch.setattr(oauth_mod, "exchange_code_for_tokens", fake_exchange)
        monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda url: None)  # never actually open a browser

        try:
            with app.app_context():
                result = oauth_mod.start_oauth_flow(
                    "oauth-loop1", "https://provider.example/authorize", "https://provider.example/token",
                    "cid", "secret", "some.scope",
                )
            flow_id = result["flow_id"]
            with oauth_mod._flows_lock:
                flow = oauth_mod._flows[flow_id]
            port = flow["port"]
            state = flow["state"]

            # Simulate the provider's redirect hitting our loopback listener.
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/callback?code=test-code&state={state}", timeout=2)
                    break
                except Exception:
                    time.sleep(0.05)

            deadline = time.time() + 5
            status = oauth_mod.get_oauth_flow_status(flow_id)
            while status["status"] not in ("connected", "error") and time.time() < deadline:
                time.sleep(0.05)
                status = oauth_mod.get_oauth_flow_status(flow_id)

            assert status["status"] == "connected", status
            assert captured_exchange["code"] == "test-code"

            with app.app_context():
                reloaded = Connector.query.get("oauth-loop1")
                assert reloaded.get_auth()["access_token"] == "a"
        finally:
            with app.app_context():
                Connector.query.filter_by(id="oauth-loop1").delete()
                db.session.commit()
            with oauth_mod._flows_lock:
                oauth_mod._flows.pop(flow_id, None)

    def test_state_mismatch_is_rejected(self, app, monkeypatch):
        with app.app_context():
            c = Connector(id="oauth-loop2", name="oauth-loop2", connector_type="google")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()

        monkeypatch.setattr(oauth_mod, "exchange_code_for_tokens",
                            lambda *a, **k: pytest.fail("must not exchange on state mismatch"))
        monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda url: None)

        try:
            with app.app_context():
                result = oauth_mod.start_oauth_flow(
                    "oauth-loop2", "https://provider.example/authorize", "https://provider.example/token",
                    "cid", "secret", "some.scope",
                )
            flow_id = result["flow_id"]
            with oauth_mod._flows_lock:
                port = oauth_mod._flows[flow_id]["port"]

            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/callback?code=test-code&state=WRONG-STATE", timeout=2)
                    break
                except Exception:
                    time.sleep(0.05)

            deadline = time.time() + 5
            status = oauth_mod.get_oauth_flow_status(flow_id)
            while status["status"] == "waiting" and time.time() < deadline:
                time.sleep(0.05)
                status = oauth_mod.get_oauth_flow_status(flow_id)

            assert status["status"] == "error"
            assert "state" in status["error"].lower() or "csrf" in status["error"].lower()
        finally:
            with app.app_context():
                Connector.query.filter_by(id="oauth-loop2").delete()
                db.session.commit()
            with oauth_mod._flows_lock:
                oauth_mod._flows.pop(flow_id, None)
