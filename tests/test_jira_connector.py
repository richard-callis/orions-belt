"""
Tests for the native Jira connector: connectivity test route and the
create_jira_issue/search_jira_issues MCP tools.
"""
import asyncio

import pytest

from app import db
from app.models.connector import Connector
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def jira_connector(app):
    with app.app_context():
        c = Connector(name="test-jira", connector_type="jira",
                      config='{"base_url": "https://acme.atlassian.net"}')
        c.set_auth({"email": "bot@acme.com", "api_token": "sekrit-token"})
        db.session.add(c)
        db.session.commit()
        yield c
        Connector.query.filter_by(name="test-jira").delete()
        db.session.commit()


class TestJiraConnectorType:
    def test_create_connector_accepts_jira_type(self, app, client):
        resp = client.post("/connectors/api/connectors", json={
            "name": "my-jira", "connector_type": "jira",
            "config": {"base_url": "https://acme.atlassian.net"},
            "auth": {"email": "a@b.com", "api_token": "tok"},
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["connector_type"] == "jira"
        with app.app_context():
            Connector.query.filter_by(id=data["id"]).delete()
            db.session.commit()


class TestCreateJiraIssue:
    def test_creates_issue_with_correct_request_shape(self, app, jira_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            def json(self):
                return {"key": "ENG-42"}
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
            result = _run(mcp_tools._handle_create_jira_issue("create_jira_issue", {
                "connector": "test-jira", "project_key": "ENG", "issue_type": "Bug",
                "summary": "Something broke", "description": "Steps to reproduce",
            }))

        assert "Created issue ENG-42" in result
        assert captured["url"] == "https://acme.atlassian.net/rest/api/3/issue"
        assert captured["json"]["fields"]["project"] == {"key": "ENG"}
        assert captured["json"]["fields"]["issuetype"] == {"name": "Bug"}
        assert captured["json"]["fields"]["summary"] == "Something broke"
        assert captured["json"]["fields"]["description"]["type"] == "doc"

    def test_requires_project_key_issue_type_and_summary(self, app, jira_connector):
        with app.app_context():
            missing_project = _run(mcp_tools._handle_create_jira_issue(
                "create_jira_issue", {"connector": "test-jira", "issue_type": "Bug", "summary": "s"}))
            assert "project_key is required" in missing_project

            missing_type = _run(mcp_tools._handle_create_jira_issue(
                "create_jira_issue", {"connector": "test-jira", "project_key": "ENG", "summary": "s"}))
            assert "issue_type is required" in missing_type

            missing_summary = _run(mcp_tools._handle_create_jira_issue(
                "create_jira_issue", {"connector": "test-jira", "project_key": "ENG", "issue_type": "Bug"}))
            assert "summary is required" in missing_summary

    def test_rejects_wrong_connector_type(self, app):
        with app.app_context():
            c = Connector(name="test-rest-not-jira", connector_type="rest_api",
                          config='{"base_url": "https://api.example.com"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_jira_issue("create_jira_issue", {
                    "connector": "test-rest-not-jira", "project_key": "E", "issue_type": "Bug", "summary": "s",
                }))
                assert "is not a jira connector" in result
            finally:
                Connector.query.filter_by(name="test-rest-not-jira").delete()
                db.session.commit()

    def test_missing_credentials_returns_error(self, app):
        with app.app_context():
            c = Connector(name="test-jira-nocreds", connector_type="jira",
                          config='{"base_url": "https://acme.atlassian.net"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_jira_issue("create_jira_issue", {
                    "connector": "test-jira-nocreds", "project_key": "E", "issue_type": "Bug", "summary": "s",
                }))
                assert "no email/api_token" in result
            finally:
                Connector.query.filter_by(name="test-jira-nocreds").delete()
                db.session.commit()


class TestSearchJiraIssues:
    def test_searches_and_formats_results(self, app, jira_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"issues": [
                    {"key": "ENG-1", "fields": {"summary": "First", "status": {"name": "Open"}}},
                    {"key": "ENG-2", "fields": {"summary": "Second", "status": {"name": "Done"}}},
                ]}
            text = ""

        class FakeAsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None, params=None):
                captured["url"] = url
                captured["params"] = params
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            result = _run(mcp_tools._handle_search_jira_issues("search_jira_issues", {
                "connector": "test-jira", "jql": "project = ENG",
            }))

        assert "ENG-1: First [Open]" in result
        assert "ENG-2: Second [Done]" in result
        assert captured["params"]["jql"] == "project = ENG"

    def test_requires_jql(self, app, jira_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_search_jira_issues("search_jira_issues", {
                "connector": "test-jira",
            }))
        assert "jql is required" in result

    def test_caps_max_results(self, app, jira_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"issues": []}
            text = ""

        class FakeAsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None, params=None):
                captured["params"] = params
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            _run(mcp_tools._handle_search_jira_issues("search_jira_issues", {
                "connector": "test-jira", "jql": "project = ENG", "max_results": 9999,
            }))
        assert captured["params"]["maxResults"] == mcp_tools._MAX_JIRA_SEARCH_RESULTS
