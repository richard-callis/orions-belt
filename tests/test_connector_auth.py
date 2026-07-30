"""
Tests for the connector REST bug fix (base_url + auth headers) and the
outbound SSRF/path-traversal guards added alongside it.
"""
import asyncio

import pytest

from app import db
from app.models.connector import Connector
from app.services.connector_auth import (
    build_auth_headers, is_blocked_host, validate_action_segment, validate_target_url,
)
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestBuildAuthHeaders:
    def test_bearer(self):
        assert build_auth_headers("bearer", {"token": "abc"}) == {"Authorization": "Bearer abc"}

    def test_api_key_default_header(self):
        assert build_auth_headers("api_key", {"api_key": "xyz"}) == {"X-API-Key": "xyz"}

    def test_api_key_custom_header(self):
        headers = build_auth_headers("api_key", {"api_key": "xyz", "header_name": "X-Token"})
        assert headers == {"X-Token": "xyz"}

    def test_basic(self):
        headers = build_auth_headers("basic", {"username": "u", "password": "p"})
        assert headers["Authorization"].startswith("Basic ")

    def test_none_or_missing_yields_no_headers(self):
        assert build_auth_headers("none", {}) == {}
        assert build_auth_headers("bearer", {}) == {}
        assert build_auth_headers("bearer", None) == {}


class TestValidateActionSegment:
    def test_rejects_scheme(self):
        assert validate_action_segment("http://evil.example/x") is not None

    def test_rejects_protocol_relative(self):
        assert validate_action_segment("//evil.example/x") is not None

    def test_rejects_dotdot(self):
        assert validate_action_segment("../admin") is not None

    def test_allows_plain_segment(self):
        assert validate_action_segment("issues") is None

    def test_rejects_empty(self):
        assert validate_action_segment("") is not None


class TestBlockedHost:
    def test_blocks_localhost(self):
        assert is_blocked_host("127.0.0.1") is True
        assert is_blocked_host("localhost") is True

    def test_blocks_link_local(self):
        assert is_blocked_host("169.254.169.254") is True

    def test_allows_private_lan(self):
        # This app's target use case includes on-prem/corporate REST APIs on
        # a private LAN — those must NOT be blocked.
        assert is_blocked_host("10.0.1.5") is False
        assert is_blocked_host("192.168.1.50") is False

    def test_allows_public_host(self):
        assert is_blocked_host("dev.azure.com") is False


class TestValidateTargetUrl:
    def test_blocks_loopback_url(self):
        assert validate_target_url("http://127.0.0.1:5000/api/pii/reveal/x") is not None

    def test_allows_public_url(self):
        assert validate_target_url("https://api.example.com/issues") is None


@pytest.fixture
def rest_connector(app):
    with app.app_context():
        c = Connector(
            name="test-rest",
            connector_type="rest_api",
            config='{"base_url": "https://api.example.com", "auth_type": "bearer", "method": "GET"}',
        )
        c.set_auth({"token": "sekrit"})
        db.session.add(c)
        db.session.commit()
        yield c
        Connector.query.filter_by(name="test-rest").delete()
        db.session.commit()


class TestHandleCallConnector:
    def test_uses_base_url_and_sends_auth_header(self, app, rest_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            text = "ok"

        class FakeAsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, json=None, headers=None):
                captured["method"] = method
                captured["url"] = url
                captured["headers"] = headers
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            result = _run(mcp_tools._handle_call_connector(
                "call_connector", {"connector": "test-rest", "action": "issues"}
            ))
        assert "HTTP 200" in result
        assert captured["url"] == "https://api.example.com/issues"
        assert captured["headers"] == {"Authorization": "Bearer sekrit"}

    def test_rejects_loopback_base_url(self, app, monkeypatch):
        with app.app_context():
            c = Connector(
                name="test-loopback",
                connector_type="rest_api",
                config='{"base_url": "http://127.0.0.1:5000", "auth_type": "none"}',
            )
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_call_connector(
                    "call_connector", {"connector": "test-loopback", "action": "api/pii/reveal/x"}
                ))
                assert "not allowed" in result
            finally:
                Connector.query.filter_by(name="test-loopback").delete()
                db.session.commit()

    def test_rejects_traversal_action(self, app, rest_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_call_connector(
                "call_connector", {"connector": "test-rest", "action": "../admin"}
            ))
            assert "must not contain" in result
