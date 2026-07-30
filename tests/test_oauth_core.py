"""
Tests for the shared OAuth core (app/services/oauth.py) — PKCE generation,
the real loopback HTTP listener (a genuine local socket, no live provider
needed), token exchange/refresh, and get_valid_access_token's refresh
decision logic. The one thing NOT testable here is the actual browser-based
consent step against a real provider.
"""
import socket
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

    def test_sends_pkce_code_verifier_in_the_request_body(self, monkeypatch):
        # PKCE is the headline security property of this flow — without the
        # code_verifier actually reaching the token endpoint, a stolen
        # authorization code would be redeemable by anyone, not just the
        # party that generated the matching verifier.
        class FakeResp:
            status_code = 200
            def json(self):
                return {"access_token": "abc", "refresh_token": "def", "expires_in": 3600}
            text = ""

        captured = {}
        def fake_post(url, data=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return FakeResp()

        monkeypatch.setattr("httpx.post", fake_post)
        oauth_mod.exchange_code_for_tokens(
            "https://x/token", "cid", "secret", "http://x/callback", "auth-code", "the-verifier")
        assert captured["url"] == "https://x/token"
        assert captured["data"]["grant_type"] == "authorization_code"
        assert captured["data"]["code_verifier"] == "the-verifier"
        assert captured["data"]["code"] == "auth-code"
        assert captured["data"]["redirect_uri"] == "http://x/callback"
        assert captured["data"]["client_id"] == "cid"
        assert captured["data"]["client_secret"] == "secret"


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

    def test_refresh_persists_instance_url_when_provider_returns_one(self, app, monkeypatch):
        # Salesforce returns the org's real API host alongside refreshed
        # tokens — it must be persisted so callers building REST URLs get
        # the actual org host, not whatever generic domain (e.g.
        # login.salesforce.com) the connector happened to be configured with.
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(oauth_mod, "refresh_access_token", lambda *a, **k: {
            "access_token": "refreshed", "refresh_token": "r2", "expires_in": 3600,
            "instance_url": "https://acme-org.my.salesforce.com",
        })
        with app.app_context():
            soon = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
            c = self._make_connector("oauth-c2b", {
                "access_token": "stale", "refresh_token": "r", "expires_at": soon,
                "client_id": "cid", "client_secret": "secret",
            })
            db.session.add(c)
            db.session.commit()
            try:
                oauth_mod.get_valid_access_token(c, "https://x/token")
                reloaded = Connector.query.get("oauth-c2b")
                assert reloaded.get_auth()["instance_url"] == "https://acme-org.my.salesforce.com"
            finally:
                Connector.query.filter_by(id="oauth-c2b").delete()
                db.session.commit()

    def test_refresh_persists_granted_scope_when_provider_returns_one(self, app, monkeypatch):
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(oauth_mod, "refresh_access_token", lambda *a, **k: {
            "access_token": "refreshed", "refresh_token": "r2", "expires_in": 3600,
            "scope": "offline_access Calendars.ReadWrite",
        })
        with app.app_context():
            soon = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
            c = self._make_connector("oauth-c2c", {
                "access_token": "stale", "refresh_token": "r", "expires_at": soon,
                "client_id": "cid", "client_secret": "secret",
            })
            db.session.add(c)
            db.session.commit()
            try:
                oauth_mod.get_valid_access_token(c, "https://x/token")
                reloaded = Connector.query.get("oauth-c2c")
                assert reloaded.get_auth()["granted_scope"] == "offline_access Calendars.ReadWrite"
            finally:
                Connector.query.filter_by(id="oauth-c2c").delete()
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


class TestGetValidAccessTokenConcurrency:
    def test_concurrent_refresh_calls_only_hit_the_provider_once(self, app):
        """Several providers this app talks to (Salesforce, Graph) rotate
        the refresh token on every use. Two callers racing an unsynchronized
        refresh would both send the same (about-to-be-invalidated) refresh
        token; the loser's response either fails outright or persists a
        token the provider has already superseded. Only one actual refresh
        call should happen per near-expiry window, no matter how many
        threads observe "needs refresh" at once."""
        import threading
        from datetime import datetime, timedelta, timezone

        call_count = {"n": 0}
        call_lock = threading.Lock()

        def fake_refresh(token_endpoint, client_id, client_secret, refresh_token):
            with call_lock:
                call_count["n"] += 1
            time.sleep(0.15)  # widen the race window so unsynchronized callers would overlap
            return {"access_token": "refreshed-once", "refresh_token": "r2", "expires_in": 3600}

        with app.app_context():
            soon = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
            c = Connector(id="oauth-race1", name="oauth-race1", connector_type="google")
            c.set_auth({
                "access_token": "stale", "refresh_token": "r", "expires_at": soon,
                "client_id": "cid", "client_secret": "secret",
            })
            db.session.add(c)
            db.session.commit()

        real_refresh = oauth_mod.refresh_access_token
        oauth_mod.refresh_access_token = fake_refresh

        results = []
        errors = []

        def worker():
            try:
                with app.app_context():
                    conn = Connector.query.filter_by(id="oauth-race1").first()
                    token = oauth_mod.get_valid_access_token(conn, "https://x/token")
                    results.append(token)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors, errors
            assert call_count["n"] == 1, f"expected exactly 1 refresh call, got {call_count['n']}"
            assert results == ["refreshed-once"] * 4
        finally:
            oauth_mod.refresh_access_token = real_refresh
            with app.app_context():
                Connector.query.filter_by(id="oauth-race1").delete()
                db.session.commit()
            with oauth_mod._connector_locks_meta_lock:
                oauth_mod._connector_locks.pop("oauth-race1", None)


class TestStoreTokens:
    def test_persists_instance_url_when_present(self, app):
        with app.app_context():
            c = Connector(id="oauth-store1", name="oauth-store1", connector_type="salesforce")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()
            try:
                oauth_mod._store_tokens("oauth-store1", {
                    "access_token": "a", "refresh_token": "b", "expires_in": 3600,
                    "instance_url": "https://acme-org.my.salesforce.com",
                })
                reloaded = Connector.query.get("oauth-store1")
                assert reloaded.get_auth()["instance_url"] == "https://acme-org.my.salesforce.com"
            finally:
                Connector.query.filter_by(id="oauth-store1").delete()
                db.session.commit()

    def test_no_instance_url_key_added_when_provider_omits_it(self, app):
        with app.app_context():
            c = Connector(id="oauth-store2", name="oauth-store2", connector_type="google")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()
            try:
                oauth_mod._store_tokens("oauth-store2", {
                    "access_token": "a", "refresh_token": "b", "expires_in": 3600,
                })
                reloaded = Connector.query.get("oauth-store2")
                assert "instance_url" not in reloaded.get_auth()
            finally:
                Connector.query.filter_by(id="oauth-store2").delete()
                db.session.commit()

    def test_persists_granted_scope_when_present(self, app):
        with app.app_context():
            c = Connector(id="oauth-store3", name="oauth-store3", connector_type="microsoft_graph")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()
            try:
                oauth_mod._store_tokens("oauth-store3", {
                    "access_token": "a", "refresh_token": "b", "expires_in": 3600,
                    "scope": "offline_access ChannelMessage.Send Calendars.ReadWrite",
                })
                reloaded = Connector.query.get("oauth-store3")
                assert reloaded.get_auth()["granted_scope"] == "offline_access ChannelMessage.Send Calendars.ReadWrite"
            finally:
                Connector.query.filter_by(id="oauth-store3").delete()
                db.session.commit()

    def test_no_granted_scope_key_added_when_provider_omits_it(self, app):
        with app.app_context():
            c = Connector(id="oauth-store4", name="oauth-store4", connector_type="google")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()
            try:
                oauth_mod._store_tokens("oauth-store4", {
                    "access_token": "a", "refresh_token": "b", "expires_in": 3600,
                })
                reloaded = Connector.query.get("oauth-store4")
                assert "granted_scope" not in reloaded.get_auth()
            finally:
                Connector.query.filter_by(id="oauth-store4").delete()
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

    def test_binds_loopback_only_not_all_interfaces(self, app, monkeypatch):
        with app.app_context():
            c = Connector(id="oauth-loop3", name="oauth-loop3", connector_type="google")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()

        monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda url: None)
        try:
            with app.app_context():
                result = oauth_mod.start_oauth_flow(
                    "oauth-loop3", "https://provider.example/authorize", "https://provider.example/token",
                    "cid", "secret", "some.scope",
                )
            flow_id = result["flow_id"]
            with oauth_mod._flows_lock:
                port = oauth_mod._flows[flow_id]["port"]

            deadline = time.time() + 5
            server_address = None
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2) as s:
                        server_address = s.getpeername()
                    break
                except Exception:
                    time.sleep(0.05)
            assert server_address is not None, "loopback listener never came up"
            assert server_address[0] == "127.0.0.1"
        finally:
            with app.app_context():
                Connector.query.filter_by(id="oauth-loop3").delete()
                db.session.commit()
            with oauth_mod._flows_lock:
                oauth_mod._flows.pop(flow_id, None)

    def test_stray_request_before_callback_does_not_kill_the_flow(self, app, monkeypatch):
        # handle_request() used to be called exactly once — ANY request that
        # reached the loopback port first (a favicon fetch, a browser
        # prefetch, a stray probe) consumed that single call and dropped the
        # flow into "timed out" while the user was still on the provider's
        # actual consent screen. The listener must keep waiting past a
        # request that isn't the real /callback.
        with app.app_context():
            c = Connector(id="oauth-loop4", name="oauth-loop4", connector_type="google")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()

        monkeypatch.setattr(oauth_mod, "exchange_code_for_tokens",
                            lambda *a, **k: {"access_token": "a", "refresh_token": "b", "expires_in": 3600})
        monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda url: None)

        try:
            with app.app_context():
                result = oauth_mod.start_oauth_flow(
                    "oauth-loop4", "https://provider.example/authorize", "https://provider.example/token",
                    "cid", "secret", "some.scope",
                )
            flow_id = result["flow_id"]
            with oauth_mod._flows_lock:
                flow = oauth_mod._flows[flow_id]
            port = flow["port"]
            state = flow["state"]

            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=2)
                    break
                except Exception:
                    time.sleep(0.05)

            # Give the listener a moment to have processed (and survived)
            # the stray request before sending the real callback.
            time.sleep(0.2)
            assert oauth_mod.get_oauth_flow_status(flow_id)["status"] == "waiting"

            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=test-code&state={state}", timeout=2)

            deadline = time.time() + 5
            status = oauth_mod.get_oauth_flow_status(flow_id)
            while status["status"] not in ("connected", "error") and time.time() < deadline:
                time.sleep(0.05)
                status = oauth_mod.get_oauth_flow_status(flow_id)

            assert status["status"] == "connected", status
        finally:
            with app.app_context():
                Connector.query.filter_by(id="oauth-loop4").delete()
                db.session.commit()
            with oauth_mod._flows_lock:
                oauth_mod._flows.pop(flow_id, None)

    def test_callback_handler_has_a_bounded_socket_timeout(self):
        # A behavioral test alone can't distinguish "the fix is in source"
        # from "this test's own monkeypatch happens to set one" — _CallbackHandler
        # inherits BaseRequestHandler.timeout = None either way, so
        # monkeypatch.setattr succeeds (and masks a missing fix) regardless.
        # Pin the actual configured default directly.
        assert oauth_mod._CallbackHandler.timeout is not None
        assert oauth_mod._CallbackHandler.timeout <= 30

    def test_connected_but_silent_client_does_not_hang_the_listener(self, app, monkeypatch):
        # server.timeout only bounds the wait for a NEW connection to arrive
        # — once accepted, a client that opens the socket and never sends a
        # request line would otherwise block handle_request() forever
        # (StreamRequestHandler has no per-connection read timeout by
        # default), starving both the overall flow deadline and the
        # eventual /callback from ever being processed. Shrink the handler's
        # timeout so this test doesn't have to wait out the real default.
        monkeypatch.setattr(oauth_mod._CallbackHandler, "timeout", 0.3)
        with app.app_context():
            c = Connector(id="oauth-loop5", name="oauth-loop5", connector_type="google")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()

        monkeypatch.setattr(oauth_mod, "exchange_code_for_tokens",
                            lambda *a, **k: {"access_token": "a", "refresh_token": "b", "expires_in": 3600})
        monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda url: None)

        try:
            with app.app_context():
                result = oauth_mod.start_oauth_flow(
                    "oauth-loop5", "https://provider.example/authorize", "https://provider.example/token",
                    "cid", "secret", "some.scope",
                )
            flow_id = result["flow_id"]
            with oauth_mod._flows_lock:
                flow = oauth_mod._flows[flow_id]
            port = flow["port"]
            state = flow["state"]

            # Connect and send nothing — never even a request line. Retry:
            # the real HTTPServer binds asynchronously inside the background
            # listener thread start_oauth_flow() just kicked off, so the
            # port may not be accepting connections yet the instant this
            # test resumes (same race every other real-socket test in this
            # file already retries around).
            silent = None
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    silent = socket.create_connection(("127.0.0.1", port), timeout=2)
                    break
                except OSError:
                    time.sleep(0.05)
            assert silent is not None, "loopback listener never came up"
            try:
                time.sleep(1)  # well past the shrunk 0.3s handler timeout
                # The listener must have moved on and still be waiting for
                # the real callback, not stuck inside the silent connection.
                assert oauth_mod.get_oauth_flow_status(flow_id)["status"] == "waiting"
            finally:
                silent.close()

            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=test-code&state={state}", timeout=2)

            deadline = time.time() + 5
            status = oauth_mod.get_oauth_flow_status(flow_id)
            while status["status"] not in ("connected", "error") and time.time() < deadline:
                time.sleep(0.05)
                status = oauth_mod.get_oauth_flow_status(flow_id)

            assert status["status"] == "connected", status
        finally:
            with app.app_context():
                Connector.query.filter_by(id="oauth-loop5").delete()
                db.session.commit()
            with oauth_mod._flows_lock:
                oauth_mod._flows.pop(flow_id, None)


class TestScheduleFlowCleanup:
    def test_flow_removed_after_delay(self):
        with oauth_mod._flows_lock:
            oauth_mod._flows["cleanup-test-1"] = {"status": "connected"}
        try:
            oauth_mod._schedule_flow_cleanup("cleanup-test-1", delay=0.05)
            deadline = time.time() + 3
            removed = False
            while time.time() < deadline:
                with oauth_mod._flows_lock:
                    removed = "cleanup-test-1" not in oauth_mod._flows
                if removed:
                    break
                time.sleep(0.02)
            assert removed, "flow was never cleaned up"
        finally:
            with oauth_mod._flows_lock:
                oauth_mod._flows.pop("cleanup-test-1", None)
