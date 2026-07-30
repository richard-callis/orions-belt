"""
Tests for the native Linear connector: connectivity test route and the
create_linear_issue/search_linear_issues MCP tools.
"""
import asyncio

import pytest

from app import db
from app.models.connector import Connector
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def linear_connector(app):
    with app.app_context():
        c = Connector(name="test-linear", connector_type="linear", config="{}")
        c.set_auth({"api_key": "lin_api_sekrit"})
        db.session.add(c)
        db.session.commit()
        yield c
        Connector.query.filter_by(name="test-linear").delete()
        db.session.commit()


class TestLinearConnectorType:
    def test_create_connector_accepts_linear_type(self, app, client):
        resp = client.post("/connectors/api/connectors", json={
            "name": "my-linear", "connector_type": "linear", "auth": {"api_key": "lin_x"},
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["connector_type"] == "linear"
        with app.app_context():
            Connector.query.filter_by(id=data["id"]).delete()
            db.session.commit()


class TestCreateLinearIssue:
    def test_creates_issue_with_correct_request_shape(self, app, linear_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"data": {"issueCreate": {"success": True,
                        "issue": {"identifier": "ENG-7", "url": "https://linear.app/acme/issue/ENG-7"}}}}
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
            result = _run(mcp_tools._handle_create_linear_issue("create_linear_issue", {
                "connector": "test-linear", "team_id": "team-123", "title": "Fix the bug",
                "description": "Details here",
            }))

        assert "Created issue ENG-7" in result
        assert captured["url"] == "https://api.linear.app/graphql"
        assert captured["headers"]["Authorization"] == "lin_api_sekrit"  # no "Bearer " prefix
        assert captured["json"]["variables"]["input"] == {
            "teamId": "team-123", "title": "Fix the bug", "description": "Details here",
        }

    def test_requires_team_id_and_title(self, app, linear_connector):
        with app.app_context():
            missing_team = _run(mcp_tools._handle_create_linear_issue(
                "create_linear_issue", {"connector": "test-linear", "title": "t"}))
            assert "team_id is required" in missing_team

            missing_title = _run(mcp_tools._handle_create_linear_issue(
                "create_linear_issue", {"connector": "test-linear", "team_id": "x"}))
            assert "title is required" in missing_title

    def test_surfaces_graphql_errors(self, app, linear_connector, monkeypatch):
        class FakeResponse:
            status_code = 200
            def json(self):
                return {"errors": [{"message": "Team not found"}]}
            text = ""

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
            result = _run(mcp_tools._handle_create_linear_issue("create_linear_issue", {
                "connector": "test-linear", "team_id": "bad-team", "title": "t",
            }))
        assert "errors" in result.lower()

    def test_rejects_wrong_connector_type(self, app):
        with app.app_context():
            c = Connector(name="test-rest-not-linear", connector_type="rest_api",
                          config='{"base_url": "https://api.example.com"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_linear_issue("create_linear_issue", {
                    "connector": "test-rest-not-linear", "team_id": "t", "title": "t",
                }))
                assert "is not a linear connector" in result
            finally:
                Connector.query.filter_by(name="test-rest-not-linear").delete()
                db.session.commit()


class TestSearchLinearIssues:
    def test_searches_and_formats_results(self, app, linear_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"data": {"issues": {"nodes": [
                    {"identifier": "ENG-1", "title": "First", "state": {"name": "Todo"}},
                    {"identifier": "ENG-2", "title": "Second", "state": {"name": "Done"}},
                ]}}}
            text = ""

        class FakeAsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                captured["json"] = json
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            result = _run(mcp_tools._handle_search_linear_issues("search_linear_issues", {
                "connector": "test-linear", "query": "bug",
            }))

        assert "ENG-1: First [Todo]" in result
        assert "ENG-2: Second [Done]" in result
        assert captured["json"]["variables"]["filter"] == {"title": {"containsIgnoreCase": "bug"}}

    def test_requires_query(self, app, linear_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_search_linear_issues("search_linear_issues", {
                "connector": "test-linear",
            }))
        assert "query is required" in result

    def test_no_results(self, app, linear_connector, monkeypatch):
        class FakeResponse:
            status_code = 200
            def json(self):
                return {"data": {"issues": {"nodes": []}}}
            text = ""

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
            result = _run(mcp_tools._handle_search_linear_issues("search_linear_issues", {
                "connector": "test-linear", "query": "nonexistent",
            }))
        assert "No issues found" in result
