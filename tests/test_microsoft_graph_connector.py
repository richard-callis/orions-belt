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

    async def get(self, url, headers=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
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

    def test_rejects_path_traversal_in_team_or_channel_id(self, app, graph_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_post_teams_message("post_teams_message", {
                "connector": "test-graph", "team_id": "../other-team", "channel_id": "c", "message": "m",
            }))
            assert "must not contain" in result

            result2 = _run(mcp_tools._handle_post_teams_message("post_teams_message", {
                "connector": "test-graph", "team_id": "t", "channel_id": "c/../../secret", "message": "m",
            }))
            assert "must not contain" in result2

    def test_allows_realistic_graph_channel_id_with_colon_and_at(self, app, graph_connector, monkeypatch):
        # Real Microsoft Graph channel ids look like "19:abc123@thread.tacv2"
        # — the path-segment check must not reject legitimate ids just
        # because they contain characters beyond plain alnum/dash.
        captured = {}

        class FakeResponse:
            status_code = 201
            text = ""

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(captured, FakeResponse()))

        with app.app_context():
            result = _run(mcp_tools._handle_post_teams_message("post_teams_message", {
                "connector": "test-graph", "team_id": "team-1",
                "channel_id": "19:abc123@thread.tacv2", "message": "hi",
            }))
        assert "Posted message" in result
        assert "19:abc123@thread.tacv2" in captured["url"]

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


class TestGraphScopeString:
    def test_defaults_to_teams_and_planner_when_no_scopes_configured(self):
        from app.services.oauth_providers import get_provider_config
        cfg = get_provider_config("microsoft_graph", {})
        assert "ChannelMessage.Send" in cfg["scope"]
        assert "Tasks.ReadWrite" in cfg["scope"]
        assert "Calendars.ReadWrite" not in cfg["scope"]
        assert "Files.ReadWrite" not in cfg["scope"]
        assert "offline_access" in cfg["scope"]

    def test_composes_from_explicit_scopes(self):
        from app.services.oauth_providers import get_provider_config
        cfg = get_provider_config("microsoft_graph", {"scopes": ["calendar", "files"]})
        assert "Calendars.ReadWrite" in cfg["scope"]
        assert "Files.ReadWrite" in cfg["scope"]
        assert "ChannelMessage.Send" not in cfg["scope"]
        assert "Tasks.ReadWrite" not in cfg["scope"]

    def test_ignores_unknown_scope_keys(self):
        from app.services.oauth_providers import get_provider_config
        cfg = get_provider_config("microsoft_graph", {"scopes": ["calendar", "not-a-real-feature"]})
        assert "Calendars.ReadWrite" in cfg["scope"]

    def test_explicitly_empty_scopes_does_not_fall_back_to_default(self):
        # Regression test: scopes: [] means the user deliberately selected
        # no features — an `or` on the value would treat that the same as
        # "never configured" and silently re-grant teams+planner anyway.
        from app.services.oauth_providers import get_provider_config
        cfg = get_provider_config("microsoft_graph", {"scopes": []})
        assert "ChannelMessage.Send" not in cfg["scope"]
        assert "Tasks.ReadWrite" not in cfg["scope"]
        assert "Calendars.ReadWrite" not in cfg["scope"]
        assert "Files.ReadWrite" not in cfg["scope"]
        assert cfg["scope"] == "offline_access "


class TestGraphRequiredFeatureCheck:
    def test_blocks_call_when_granted_scope_lacks_the_feature(self, app, graph_connector):
        with app.app_context():
            c = Connector.query.filter_by(name="test-graph").first()
            auth = c.get_auth()
            auth["granted_scope"] = "offline_access ChannelMessage.Send Tasks.ReadWrite"
            c.set_auth(auth)
            db.session.commit()

            token, err = mcp_tools._get_graph_connector_and_token("test-graph", required_feature="calendar")
        assert token is None
        assert "was not granted calendar access" in err
        assert "reconnect" in err.lower()

    def test_allows_call_when_granted_scope_includes_the_feature(self, app, graph_connector):
        with app.app_context():
            c = Connector.query.filter_by(name="test-graph").first()
            auth = c.get_auth()
            auth["granted_scope"] = "offline_access ChannelMessage.Send Calendars.ReadWrite"
            c.set_auth(auth)
            db.session.commit()

            token, err = mcp_tools._get_graph_connector_and_token("test-graph", required_feature="calendar")
        assert err is None
        assert token == "tok"

    def test_skips_check_when_no_granted_scope_on_file_yet(self, app, graph_connector):
        # A connector that authenticated before granted_scope started being
        # persisted has none on file — the check must not retroactively
        # block it.
        with app.app_context():
            token, err = mcp_tools._get_graph_connector_and_token("test-graph", required_feature="calendar")
        assert err is None
        assert token == "tok"


class TestCreateCalendarEvent:
    def test_creates_event_with_correct_request_shape(self, app, graph_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            def json(self):
                return {"webLink": "https://outlook.office.com/calendar/event/abc"}
            text = ""

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(captured, FakeResponse()))

        with app.app_context():
            result = _run(mcp_tools._handle_create_calendar_event("create_calendar_event", {
                "connector": "test-graph", "subject": "Planning sync",
                "start": "2026-08-01T14:00:00", "end": "2026-08-01T14:30:00",
                "attendees": ["a@example.com", "b@example.com"],
            }))

        assert "Created calendar event: Planning sync" in result
        assert captured["url"] == "https://graph.microsoft.com/v1.0/me/events"
        assert captured["json"]["subject"] == "Planning sync"
        assert len(captured["json"]["attendees"]) == 2

    def test_requires_subject_start_and_end(self, app, graph_connector):
        with app.app_context():
            r1 = _run(mcp_tools._handle_create_calendar_event("create_calendar_event", {
                "connector": "test-graph", "start": "x", "end": "y",
            }))
            assert "subject is required" in r1

            r2 = _run(mcp_tools._handle_create_calendar_event("create_calendar_event", {
                "connector": "test-graph", "subject": "s", "end": "y",
            }))
            assert "start is required" in r2

            r3 = _run(mcp_tools._handle_create_calendar_event("create_calendar_event", {
                "connector": "test-graph", "subject": "s", "start": "x",
            }))
            assert "end is required" in r3


class TestCheckCalendarAvailability:
    def test_reports_availability(self, app, graph_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"value": [{"scheduleId": "a@example.com", "availabilityView": "000222"}]}
            text = ""

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(captured, FakeResponse()))

        with app.app_context():
            result = _run(mcp_tools._handle_check_calendar_availability("check_calendar_availability", {
                "connector": "test-graph", "attendees": ["a@example.com"],
                "start": "2026-08-01T09:00:00", "end": "2026-08-01T17:00:00",
            }))

        assert "a@example.com: 000222" in result
        assert captured["url"] == "https://graph.microsoft.com/v1.0/me/calendar/getSchedule"

    def test_requires_nonempty_attendees(self, app, graph_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_check_calendar_availability("check_calendar_availability", {
                "connector": "test-graph", "attendees": [], "start": "x", "end": "y",
            }))
        assert "attendees is required" in result


class _FakeMultiCallAsyncClient:
    """Like _FakeAsyncClient but returns a different response per call,
    consumed in order — needed for create_onedrive_file's existence-check
    GET followed by its PUT."""
    def __init__(self, captured, responses):
        self._captured = captured
        self._responses = list(responses)

    def __call__(self, timeout=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        self._captured.setdefault("calls", []).append(("GET", url))
        return self._responses.pop(0)

    async def put(self, url, headers=None, content=None):
        self._captured.setdefault("calls", []).append(("PUT", url))
        self._captured["put_content"] = content
        return self._responses.pop(0)


class TestReadOnedriveFile:
    def test_reads_file_content(self, app, graph_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            text = "file contents here"

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(captured, FakeResponse()))

        with app.app_context():
            result = _run(mcp_tools._handle_read_onedrive_file("read_onedrive_file", {
                "connector": "test-graph", "path": "Documents/notes.txt",
            }))

        assert result == "file contents here"
        assert "Documents/notes.txt" in captured["url"] or "Documents%2Fnotes.txt" in captured["url"]

    def test_requires_path(self, app, graph_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_read_onedrive_file("read_onedrive_file", {
                "connector": "test-graph",
            }))
        assert "path is required" in result

    def test_returns_error_on_404(self, app, graph_connector, monkeypatch):
        class FakeResponse:
            status_code = 404
            text = "Not Found"

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient({}, FakeResponse()))

        with app.app_context():
            result = _run(mcp_tools._handle_read_onedrive_file("read_onedrive_file", {
                "connector": "test-graph", "path": "missing.txt",
            }))
        assert "file not found" in result


class TestCreateOnedriveFile:
    def test_creates_file_when_none_exists(self, app, graph_connector, monkeypatch):
        captured = {}

        class FakeGetResponse:
            status_code = 404
            text = "Not Found"

        class FakePutResponse:
            status_code = 201
            def json(self):
                return {"webUrl": "https://acme.sharepoint.com/personal/x/Documents/new.txt"}
            text = ""

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient",
                            _FakeMultiCallAsyncClient(captured, [FakeGetResponse(), FakePutResponse()]))

        with app.app_context():
            result = _run(mcp_tools._handle_create_onedrive_file("create_onedrive_file", {
                "connector": "test-graph", "path": "Documents/new.txt", "content": "hello",
            }))

        assert "Created OneDrive file: Documents/new.txt" in result
        assert captured["calls"][0][0] == "GET"
        assert captured["calls"][1][0] == "PUT"
        assert captured["put_content"] == b"hello"

    def test_refuses_when_file_already_exists(self, app, graph_connector, monkeypatch):
        captured = {}

        class FakeGetResponse:
            status_code = 200
            text = ""

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeMultiCallAsyncClient(captured, [FakeGetResponse()]))

        with app.app_context():
            result = _run(mcp_tools._handle_create_onedrive_file("create_onedrive_file", {
                "connector": "test-graph", "path": "Documents/existing.txt", "content": "x",
            }))

        assert "already exists" in result
        assert len(captured["calls"]) == 1  # never reached the PUT

    def test_requires_path(self, app, graph_connector):
        with app.app_context():
            result = _run(mcp_tools._handle_create_onedrive_file("create_onedrive_file", {
                "connector": "test-graph", "content": "x",
            }))
        assert "path is required" in result

    def test_refuses_to_overwrite_when_existence_check_is_inconclusive(self, app, graph_connector, monkeypatch):
        """A non-200, non-404 response from the existence-check GET (rate
        limited, transient 5xx, ...) means we genuinely don't know if the
        file exists — must refuse rather than fall through to an
        unconditional PUT that could silently overwrite a real file."""
        captured = {}

        class FakeGetResponse:
            status_code = 429
            text = "Too Many Requests"

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeMultiCallAsyncClient(captured, [FakeGetResponse()]))

        with app.app_context():
            result = _run(mcp_tools._handle_create_onedrive_file("create_onedrive_file", {
                "connector": "test-graph", "path": "Documents/maybe-exists.txt", "content": "x",
            }))

        assert result.startswith("Error")
        assert len(captured["calls"]) == 1  # never reached the PUT

    def test_requires_files_scope(self, app, graph_connector):
        with app.app_context():
            c = Connector.query.filter_by(name="test-graph").first()
            auth = c.get_auth()
            auth["granted_scope"] = "offline_access ChannelMessage.Send"
            c.set_auth(auth)
            db.session.commit()

            result = _run(mcp_tools._handle_read_onedrive_file("read_onedrive_file", {
                "connector": "test-graph", "path": "x.txt",
            }))
        assert "was not granted files access" in result
