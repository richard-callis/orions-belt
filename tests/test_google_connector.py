"""
Tests for the Google connector's create_google_task MCP tool. The OAuth
consent flow itself is covered by test_oauth_core.py and
test_oauth_connector_routes.py — this file covers the tool that uses an
already-connected google connector's stored tokens.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.models.connector import Connector
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def google_connector(app):
    with app.app_context():
        c = Connector(name="test-google", connector_type="google", config="{}")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        c.set_auth({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "tok", "refresh_token": "r", "expires_at": future,
        })
        db.session.add(c)
        db.session.commit()
        yield c
        Connector.query.filter_by(name="test-google").delete()
        db.session.commit()


class TestCreateGoogleTask:
    def test_creates_task_with_correct_request_shape(self, app, google_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"id": "abc123", "selfLink": "https://tasks.googleapis.com/tasks/v1/lists/x/tasks/abc123"}
            text = ""

        class FakeAsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            result = _run(mcp_tools._handle_create_google_task("create_google_task", {
                "connector": "test-google", "title": "Buy milk", "notes": "2%",
            }))

        assert "Created Google Task: Buy milk" in result
        assert captured["url"] == "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks"
        assert captured["headers"]["Authorization"] == "Bearer tok"
        assert captured["json"] == {"title": "Buy milk", "notes": "2%"}

    def test_requires_connector_and_title(self, app, google_connector):
        with app.app_context():
            missing_connector = _run(mcp_tools._handle_create_google_task(
                "create_google_task", {"title": "t"}))
            assert "connector name is required" in missing_connector

            missing_title = _run(mcp_tools._handle_create_google_task(
                "create_google_task", {"connector": "test-google"}))
            assert "title is required" in missing_title

    def test_rejects_wrong_connector_type(self, app):
        with app.app_context():
            c = Connector(name="test-rest-not-google", connector_type="rest_api",
                          config='{"base_url": "https://api.example.com"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_google_task("create_google_task", {
                    "connector": "test-rest-not-google", "title": "t",
                }))
                assert "is not a google connector" in result
            finally:
                Connector.query.filter_by(name="test-rest-not-google").delete()
                db.session.commit()

    def test_never_connected_returns_reauth_error(self, app):
        with app.app_context():
            c = Connector(name="test-google-nolink", connector_type="google", config="{}")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_google_task("create_google_task", {
                    "connector": "test-google-nolink", "title": "t",
                }))
                assert "needs to be reconnected" in result
            finally:
                Connector.query.filter_by(name="test-google-nolink").delete()
                db.session.commit()

    def test_api_error_surfaces_status_and_body(self, app, google_connector, monkeypatch):
        class FakeResponse:
            status_code = 403
            text = '{"error": "insufficient scope"}'

        class FakeAsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            result = _run(mcp_tools._handle_create_google_task("create_google_task", {
                "connector": "test-google", "title": "t",
            }))
        assert "HTTP 403" in result
