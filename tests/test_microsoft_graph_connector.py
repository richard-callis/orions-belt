"""
Tests for the Microsoft Graph connector's two MCP tools —
post_teams_message and create_planner_task — which share one Graph OAuth
identity. The OAuth consent flow itself is covered by test_oauth_core.py
and test_oauth_connector_routes.py.
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
def graph_connector(app):
    with app.app_context():
        c = Connector(name="test-graph", connector_type="microsoft_graph", config='{"tenant_id": "common"}')
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        c.set_auth({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "tok", "refresh_token": "r", "expires_at": future,
        })
        db.session.add(c)
        db.session.commit()
        yield c
        Connector.query.filter_by(name="test-graph").delete()
        db.session.commit()


class _FakeAsyncClient:
    def __init__(self, captured, response):
        self._captured = captured
        self._response = response

    def __call__(self, timeout=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        self._captured["json"] = json
        return self._response


class TestPostTeamsMessage:
    def test_posts_with_correct_request_shape(self, app, graph_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            text = ""

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(captured, FakeResponse()))

        with app.app_context():
            result = _run(mcp_tools._handle_post_teams_message("post_teams_message", {
                "connector": "test-graph", "team_id": "team-1", "channel_id": "chan-1", "message": "hi",
            }))

        assert "Posted message to Teams channel chan-1" in result
        assert captured["url"] == "https://graph.microsoft.com/v1.0/teams/team-1/channels/chan-1/messages"
        assert captured["headers"]["Authorization"] == "Bearer tok"
        assert captured["json"] == {"body": {"content": "hi"}}

    def test_requires_all_args(self, app, graph_connector):
        with app.app_context():
            r1 = _run(mcp_tools._handle_post_teams_message("post_teams_message", {"connector": "test-graph"}))
            assert "team_id is required" in r1
            r2 = _run(mcp_tools._handle_post_teams_message(
                "post_teams_message", {"connector": "test-graph", "team_id": "t"}))
            assert "channel_id is required" in r2
            r3 = _run(mcp_tools._handle_post_teams_message(
                "post_teams_message", {"connector": "test-graph", "team_id": "t", "channel_id": "c"}))
            assert "message is required" in r3

    def test_rejects_wrong_connector_type(self, app):
        with app.app_context():
            c = Connector(name="test-rest-not-graph", connector_type="rest_api",
                          config='{"base_url": "https://api.example.com"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_post_teams_message("post_teams_message", {
                    "connector": "test-rest-not-graph", "team_id": "t", "channel_id": "c", "message": "m",
                }))
                assert "is not a microsoft_graph connector" in result
            finally:
                Connector.query.filter_by(name="test-rest-not-graph").delete()
                db.session.commit()

    def test_never_connected_returns_reauth_error(self, app):
        with app.app_context():
            c = Connector(name="test-graph-nolink", connector_type="microsoft_graph", config="{}")
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_post_teams_message("post_teams_message", {
                    "connector": "test-graph-nolink", "team_id": "t", "channel_id": "c", "message": "m",
                }))
                assert "needs to be reconnected" in result
            finally:
                Connector.query.filter_by(name="test-graph-nolink").delete()
                db.session.commit()


class TestCreatePlannerTask:
    def test_creates_task_with_correct_request_shape(self, app, graph_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            def json(self):
                return {"id": "task-123"}
            text = ""

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(captured, FakeResponse()))

        with app.app_context():
            result = _run(mcp_tools._handle_create_planner_task("create_planner_task", {
                "connector": "test-graph", "plan_id": "plan-1", "title": "Ship it", "bucket_id": "bucket-1",
            }))

        assert "Created Planner task: Ship it" in result
        assert captured["url"] == "https://graph.microsoft.com/v1.0/planner/tasks"
        assert captured["json"] == {"planId": "plan-1", "title": "Ship it", "bucketId": "bucket-1"}

    def test_requires_connector_plan_id_and_title(self, app, graph_connector):
        with app.app_context():
            r1 = _run(mcp_tools._handle_create_planner_task("create_planner_task", {"title": "t"}))
            assert "connector name is required" in r1
            r2 = _run(mcp_tools._handle_create_planner_task(
                "create_planner_task", {"connector": "test-graph", "title": "t"}))
            assert "plan_id is required" in r2
            r3 = _run(mcp_tools._handle_create_planner_task(
                "create_planner_task", {"connector": "test-graph", "plan_id": "p"}))
            assert "title is required" in r3

    def test_api_error_surfaces_status_and_body(self, app, graph_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 403
            text = '{"error": "insufficient scope"}'

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(captured, FakeResponse()))

        with app.app_context():
            result = _run(mcp_tools._handle_create_planner_task("create_planner_task", {
                "connector": "test-graph", "plan_id": "p", "title": "t",
            }))
        assert "HTTP 403" in result
