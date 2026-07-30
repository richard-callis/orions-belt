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

    def test_rejects_url_structural_characters(self):
        # A value like "acme?x=" would still truncate the interpolated URL
        # at the query-string boundary and redirect the credentialed request
        # to a different path on the same trusted host — the '/'/'..' block
        # alone doesn't stop that.
        assert mcp_tools._is_safe_path_segment("acme?x=y") is False
        assert mcp_tools._is_safe_path_segment("acme#frag") is False
        assert mcp_tools._is_safe_path_segment("acme%2F..%2Fsecret") is False


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


class TestCreateGithubPr:
    def test_creates_pr_with_correct_request_shape(self, app, github_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            def json(self):
                return {"number": 42, "html_url": "https://github.com/acme/widget/pull/42"}
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
            result = _run(mcp_tools._handle_create_github_pr("create_github_pr", {
                "connector": "test-github", "owner": "acme", "repo": "widget",
                "title": "Fix the thing", "head": "feature-branch", "base": "main",
                "body": "Fixes #7",
            }))

        assert "Created PR #42" in result
        assert captured["url"] == "https://api.github.com/repos/acme/widget/pulls"
        assert captured["json"] == {"title": "Fix the thing", "head": "feature-branch",
                                    "base": "main", "body": "Fixes #7"}

    def test_requires_head_and_base(self, app, github_connector):
        with app.app_context():
            missing_head = _run(mcp_tools._handle_create_github_pr("create_github_pr", {
                "connector": "test-github", "owner": "o", "repo": "r", "title": "t", "base": "main",
            }))
            assert "head is required" in missing_head

            missing_base = _run(mcp_tools._handle_create_github_pr("create_github_pr", {
                "connector": "test-github", "owner": "o", "repo": "r", "title": "t", "head": "feature",
            }))
            assert "base is required" in missing_base

    def test_rejects_path_traversal_in_owner_or_repo(self, app, github_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_create_github_pr("create_github_pr", {
                "connector": "test-github", "owner": "../evil", "repo": "widget",
                "title": "t", "head": "h", "base": "main",
            }))
            assert "must not contain" in result


class TestCommentOnGithubPr:
    def test_posts_comment_with_correct_request_shape(self, app, github_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            def json(self):
                return {"html_url": "https://github.com/acme/widget/pull/7#issuecomment-1"}
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
                captured["json"] = json
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            result = _run(mcp_tools._handle_comment_on_github_pr("comment_on_github_pr", {
                "connector": "test-github", "owner": "acme", "repo": "widget",
                "pr_number": 7, "body": "Looks good to me",
            }))

        assert "Commented on PR #7" in result
        assert captured["url"] == "https://api.github.com/repos/acme/widget/issues/7/comments"
        assert captured["json"] == {"body": "Looks good to me"}

    def test_requires_pr_number_and_body(self, app, github_connector):
        with app.app_context():
            missing_pr = _run(mcp_tools._handle_comment_on_github_pr("comment_on_github_pr", {
                "connector": "test-github", "owner": "o", "repo": "r", "body": "x",
            }))
            assert "pr_number is required" in missing_pr

            missing_body = _run(mcp_tools._handle_comment_on_github_pr("comment_on_github_pr", {
                "connector": "test-github", "owner": "o", "repo": "r", "pr_number": 1,
            }))
            assert "body is required" in missing_body

    def test_rejects_non_integer_pr_number(self, app, github_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_comment_on_github_pr("comment_on_github_pr", {
                "connector": "test-github", "owner": "o", "repo": "r",
                "pr_number": "not-a-number", "body": "x",
            }))
            assert "must be an integer" in result


class TestGetGithubPrStatus:
    def test_reads_status_fields(self, app, github_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"state": "open", "merged": False, "mergeable": True, "mergeable_state": "clean"}
            text = ""

        class FakeAsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                captured["url"] = url
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            result = _run(mcp_tools._handle_get_github_pr_status("get_github_pr_status", {
                "connector": "test-github", "owner": "acme", "repo": "widget", "pr_number": 7,
            }))

        assert captured["url"] == "https://api.github.com/repos/acme/widget/pulls/7"
        assert "state=open" in result
        assert "merged=False" in result
        assert "mergeable_state=clean" in result

    def test_requires_pr_number(self, app, github_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_get_github_pr_status("get_github_pr_status", {
                "connector": "test-github", "owner": "o", "repo": "r",
            }))
        assert "pr_number is required" in result

    def test_surfaces_http_error(self, app, github_connector, monkeypatch):
        class FakeResponse:
            status_code = 404
            text = "Not Found"

        class FakeAsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            result = _run(mcp_tools._handle_get_github_pr_status("get_github_pr_status", {
                "connector": "test-github", "owner": "acme", "repo": "widget", "pr_number": 999,
            }))
        assert "HTTP 404" in result
