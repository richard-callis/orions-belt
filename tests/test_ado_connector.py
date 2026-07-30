"""
Tests for the Azure DevOps connector: connectivity test route and the
create_ado_workitem MCP tool.
"""
import asyncio

import pytest

from app import db
from app.models.connector import Connector
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def ado_connector(app):
    with app.app_context():
        c = Connector(
            name="test-ado",
            connector_type="azure_devops",
            config='{"org_url": "https://dev.azure.com/myorg"}',
        )
        c.set_auth({"pat": "sekrit-pat"})
        db.session.add(c)
        db.session.commit()
        yield c
        Connector.query.filter_by(name="test-ado").delete()
        db.session.commit()


class TestAdoConnectorType:
    def test_create_connector_accepts_azure_devops_type(self, app, client):
        resp = client.post("/connectors/api/connectors", json={
            "name": "my-ado",
            "connector_type": "azure_devops",
            "config": {"org_url": "https://dev.azure.com/myorg"},
            "auth": {"pat": "abc123"},
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["connector_type"] == "azure_devops"
        with app.app_context():
            Connector.query.filter_by(id=data["id"]).delete()
            db.session.commit()

    def test_create_connector_rejects_unknown_type(self, app, client):
        resp = client.post("/connectors/api/connectors", json={
            "name": "bad-type",
            "connector_type": "not_a_real_type",
        })
        assert resp.status_code == 400


class TestCreateAdoWorkitem:
    def test_creates_workitem_with_correct_request_shape(self, app, ado_connector, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"id": 42}
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
            result = _run(mcp_tools._handle_create_ado_workitem("create_ado_workitem", {
                "connector": "test-ado",
                "project": "My Project",
                "work_item_type": "User Story",
                "title": "Ship the widget",
                "description": "Do the thing",
            }))

        assert "Created User Story #42" in result
        assert captured["url"] == (
            "https://dev.azure.com/myorg/My%20Project/_apis/wit/workitems/"
            "$User%20Story?api-version=7.1"
        )
        assert captured["headers"]["Content-Type"] == "application/json-patch+json"
        assert captured["headers"]["Authorization"].startswith("Basic ")
        assert {"op": "add", "path": "/fields/System.Title", "value": "Ship the widget"} in captured["json"]
        assert {"op": "add", "path": "/fields/System.Description", "value": "Do the thing"} in captured["json"]

    def test_requires_project_and_type_and_title(self, app, ado_connector):
        with app.app_context():
            missing_project = _run(mcp_tools._handle_create_ado_workitem(
                "create_ado_workitem", {"connector": "test-ado", "work_item_type": "Bug", "title": "x"}))
            assert "project is required" in missing_project

            missing_type = _run(mcp_tools._handle_create_ado_workitem(
                "create_ado_workitem", {"connector": "test-ado", "project": "P", "title": "x"}))
            assert "work_item_type is required" in missing_type

            missing_title = _run(mcp_tools._handle_create_ado_workitem(
                "create_ado_workitem", {"connector": "test-ado", "project": "P", "work_item_type": "Bug"}))
            assert "title is required" in missing_title

    def test_rejects_wrong_connector_type(self, app):
        with app.app_context():
            c = Connector(name="test-rest-wrong-type", connector_type="rest_api",
                          config='{"base_url": "https://api.example.com"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_ado_workitem("create_ado_workitem", {
                    "connector": "test-rest-wrong-type", "project": "P",
                    "work_item_type": "Bug", "title": "x",
                }))
                assert "is not an azure_devops connector" in result
            finally:
                Connector.query.filter_by(name="test-rest-wrong-type").delete()
                db.session.commit()

    def test_missing_pat_returns_error(self, app):
        with app.app_context():
            c = Connector(name="test-ado-nopat", connector_type="azure_devops",
                          config='{"org_url": "https://dev.azure.com/myorg"}')
            db.session.add(c)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_ado_workitem("create_ado_workitem", {
                    "connector": "test-ado-nopat", "project": "P",
                    "work_item_type": "Bug", "title": "x",
                }))
                assert "no personal access token" in result
            finally:
                Connector.query.filter_by(name="test-ado-nopat").delete()
                db.session.commit()
