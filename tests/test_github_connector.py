"""
Tests for the GitHub connector: connectivity test route and the
create_github_issue MCP tool.
"""
import asyncio

import pytest

from app import db
from app.models.connector import Connector
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def github_connector(app):
    with app.app_context():
        c = Connector(name="test-github", connector_type="github", config="{}")
        c.set_auth({"pat": "ghp_sekrit"})
        db.session.add(c)
        db.session.commit()
        yield c
        Connector.query.filter_by(name="test-github").delete()
        db.session.commit()


class TestIsSafePathSegment:
    def test_rejects_slashes_and_traversal(self):
        assert mcp_tools._is_safe_path_segment("../x") is False
        assert mcp_tools._is_safe_path_segment("a/b") is False
        assert mcp_tools._is_safe_path_segment("a\\b") is False
        assert mcp_tools._is_safe_path_segment("") is False
        assert mcp_tools._is_safe_path_segment(" ") is False

    def test_allows_realistic_provider_ids(self):
        # GitHub owner/repo, Salesforce sobject, and a Microsoft Graph Teams
        # channel id (which legitimately contains ':' and '@') must all pass.
        assert mcp_tools._is_safe_path_segment("my-org") is True
        assert mcp_tools._is_safe_path_segment("My_Custom__c") is True
        assert mcp_tools._is_safe_path_segment("19:abc123@thread.tacv2") is True


class TestGithubConnectorType:
    def test_create_connector_accepts_github_type(self, app, client):
        resp = client.post("/connectors/api/connectors", json={
            "name": "my-github",
            "connector_type": "github",
            "auth": {"pat": "ghp_abc123"},
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["connector_type"] == "github"
        with app.app_context():
            Connector.query.filter_by(id=data["id"]).delete()
            db.session.commit()


class TestCreateGithubIssue:
    def test_creates_issue_with_correct_request_shape(self, app, github_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            def json(self):
                return {"number": 7, "html_url": "https://github.com/acme/widget/issues/7"}
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
            result = _run(mcp_tools._handle_create_github_issue("create_github_issue", {
                "connector": "test-github", "owner": "acme", "repo": "widget",
                "title": "Bug: thing broken", "body": "Steps to reproduce...",
            }))

        assert "Created issue #7" in result
        assert captured["url"] == "https://api.github.com/repos/acme/widget/issues"
        assert captured["headers"]["Authorization"] == "Bearer ghp_sekrit"
        assert captured["headers"]["Accept"] == "application/vnd.github+json"
        assert captured["json"] == {"title": "Bug: thing broken", "body": "Steps to reproduce..."}

    def test_requires_owner_repo_and_title(self, app, github_connector):
        with app.app_context():
            missing_owner = _run(mcp_tools._handle_create_github_issue(
                "create_github_issue", {"connector": "test-github", "repo": "r", "title": "t"}))
            assert "owner is required" in missing_owner

            missing_repo = _run(mcp_tools._handle_create_github_issue(
                "create_github_issue", {"connector": "test-github", "owner": "o", "title": "t"}))
            assert "repo is required" in missing_repo

            missing_title = _run(mcp_tools._handle_create_github_issue(
                "create_github_issue", {"connector": "test-github", "owner": "o", "repo": "r"}))
            assert "title is required" in missing_title

    def test_rejects_path_traversal_in_owner_or_repo(self, app, github_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_create_github_issue("create_github_issue", {
                "connector": "test-github", "owner": "../../other-org", "repo": "widget", "title": "t",
            }))
            assert "must not contain" in result

            result2 = _run(mcp_tools._handle_create_github_issue("create_github_issue", {
                "connector": "test-github", "owner": "acme", "repo": "widget/../../secret", "title": "t",
            }))
            assert "must not contain" in result2

    def test_rejects_wrong_connector_type(self, app):
        with app.app_context():
            c = Connector(name="test-rest-not-github", connector_type="rest_api",
                          config='{"base_url": "https://api.example.com"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_github_issue("create_github_issue", {
                    "connector": "test-rest-not-github", "owner": "o", "repo": "r", "title": "t",
                }))
                assert "is not a github connector" in result
            finally:
                Connector.query.filter_by(name="test-rest-not-github").delete()
                db.session.commit()

    def test_missing_pat_returns_error(self, app):
        with app.app_context():
            c = Connector(name="test-github-nopat", connector_type="github", config="{}")
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_github_issue("create_github_issue", {
                    "connector": "test-github-nopat", "owner": "o", "repo": "r", "title": "t",
                }))
                assert "no personal access token" in result
            finally:
                Connector.query.filter_by(name="test-github-nopat").delete()
                db.session.commit()
