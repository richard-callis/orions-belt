"""
Tests for the Salesforce connector's create_salesforce_record MCP tool.
The OAuth consent flow itself is covered by test_oauth_core.py and
test_oauth_connector_routes.py.
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
def salesforce_connector(app):
    with app.app_context():
        c = Connector(name="test-salesforce", connector_type="salesforce",
                      config='{"instance_url": "https://acme.my.salesforce.com"}')
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        c.set_auth({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "tok", "refresh_token": "r", "expires_at": future,
        })
        db.session.add(c)
        db.session.commit()
        yield c
        Connector.query.filter_by(name="test-salesforce").delete()
        db.session.commit()


class TestCreateSalesforceRecord:
    def test_creates_record_with_correct_request_shape(self, app, salesforce_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            def json(self):
                return {"id": "00Q5f000001abcXYZ", "success": True}
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
            result = _run(mcp_tools._handle_create_salesforce_record("create_salesforce_record", {
                "connector": "test-salesforce", "sobject_type": "Lead",
                "fields": {"LastName": "Doe", "Company": "Acme"},
            }))

        assert "Created Lead record: 00Q5f000001abcXYZ" in result
        assert captured["url"] == "https://acme.my.salesforce.com/services/data/v59.0/sobjects/Lead"
        assert captured["headers"]["Authorization"] == "Bearer tok"
        assert captured["json"] == {"LastName": "Doe", "Company": "Acme"}

    def test_requires_connector_sobject_type_and_fields(self, app, salesforce_connector):
        with app.app_context():
            r1 = _run(mcp_tools._handle_create_salesforce_record(
                "create_salesforce_record", {"sobject_type": "Lead", "fields": {"a": "b"}}))
            assert "connector name is required" in r1

            r2 = _run(mcp_tools._handle_create_salesforce_record(
                "create_salesforce_record", {"connector": "test-salesforce", "fields": {"a": "b"}}))
            assert "sobject_type is required" in r2

            r3 = _run(mcp_tools._handle_create_salesforce_record(
                "create_salesforce_record", {"connector": "test-salesforce", "sobject_type": "Lead"}))
            assert "fields is required" in r3

            r4 = _run(mcp_tools._handle_create_salesforce_record(
                "create_salesforce_record", {"connector": "test-salesforce", "sobject_type": "Lead", "fields": {}}))
            assert "fields is required" in r4

    def test_rejects_wrong_connector_type(self, app):
        with app.app_context():
            c = Connector(name="test-rest-not-sf", connector_type="rest_api",
                          config='{"base_url": "https://api.example.com"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_salesforce_record("create_salesforce_record", {
                    "connector": "test-rest-not-sf", "sobject_type": "Lead", "fields": {"a": "b"},
                }))
                assert "is not a salesforce connector" in result
            finally:
                Connector.query.filter_by(name="test-rest-not-sf").delete()
                db.session.commit()

    def test_never_connected_returns_reauth_error(self, app):
        with app.app_context():
            c = Connector(name="test-sf-nolink", connector_type="salesforce",
                          config='{"instance_url": "https://acme.my.salesforce.com"}')
            c.set_auth({"client_id": "cid", "client_secret": "secret"})
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_salesforce_record("create_salesforce_record", {
                    "connector": "test-sf-nolink", "sobject_type": "Lead", "fields": {"a": "b"},
                }))
                assert "needs to be reconnected" in result
            finally:
                Connector.query.filter_by(name="test-sf-nolink").delete()
                db.session.commit()

    def test_api_error_surfaces_status_and_body(self, app, salesforce_connector, monkeypatch):
        class FakeResponse:
            status_code = 400
            text = '[{"message": "Required fields are missing", "errorCode": "REQUIRED_FIELD_MISSING"}]'

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
            result = _run(mcp_tools._handle_create_salesforce_record("create_salesforce_record", {
                "connector": "test-salesforce", "sobject_type": "Lead", "fields": {"a": "b"},
            }))
        assert "HTTP 400" in result

    def test_defaults_to_login_salesforce_com_when_no_instance_url(self, app, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 201
            def json(self):
                return {"id": "abc"}
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
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

        with app.app_context():
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            c = Connector(name="test-sf-noinstance", connector_type="salesforce", config="{}")
            c.set_auth({
                "client_id": "cid", "client_secret": "secret",
                "access_token": "tok", "refresh_token": "r", "expires_at": future,
            })
            db.session.add(c)
            db.session.commit()
            try:
                _run(mcp_tools._handle_create_salesforce_record("create_salesforce_record", {
                    "connector": "test-sf-noinstance", "sobject_type": "Lead", "fields": {"a": "b"},
                }))
                assert captured["url"].startswith("https://login.salesforce.com")
            finally:
                Connector.query.filter_by(name="test-sf-noinstance").delete()
                db.session.commit()
