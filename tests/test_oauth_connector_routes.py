"""
Tests for the generic OAuth connector routes in app/routes/connectors.py —
create-with-new-types, /test dispatch to _test_oauth_connector, and the
/oauth/start + /oauth/status routes shared by Google/Microsoft Graph/
Salesforce. Provider-specific pieces (Google Tasks, Graph, Salesforce
actions) are covered in their own test files.
"""
import pytest

from app import db
from app.models.connector import Connector
import app.services.oauth as oauth_mod


@pytest.fixture
def google_connector(client):
    c = Connector(name="test-google-oauth", connector_type="google", config="{}")
    db.session.add(c)
    db.session.commit()
    cid = c.id
    yield cid
    Connector.query.filter_by(id=cid).delete()
    db.session.commit()


class TestToDictOAuthFields:
    def test_non_oauth_connector_has_no_oauth_fields(self, client):
        c = Connector(name="test-plain-rest", connector_type="rest_api", config='{"base_url": "https://x"}')
        db.session.add(c)
        db.session.commit()
        try:
            d = c.to_dict()
            assert "oauth_connected" not in d
            assert "oauth_client_configured" not in d
        finally:
            Connector.query.filter_by(id=c.id).delete()
            db.session.commit()

    def test_oauth_connector_reports_not_connected_and_no_credentials(self, client, google_connector):
        c = Connector.query.get(google_connector)
        d = c.to_dict()
        assert d["oauth_connected"] is False
        assert d["oauth_client_configured"] is False

    def test_oauth_connector_reports_credentials_configured_but_not_connected(self, client, google_connector):
        c = Connector.query.get(google_connector)
        c.set_auth({"client_id": "cid", "client_secret": "secret"})
        db.session.commit()
        d = c.to_dict()
        assert d["oauth_connected"] is False
        assert d["oauth_client_configured"] is True

    def test_oauth_connector_reports_connected(self, client, google_connector):
        c = Connector.query.get(google_connector)
        c.set_auth({"client_id": "cid", "client_secret": "secret", "refresh_token": "r"})
        db.session.commit()
        d = c.to_dict()
        assert d["oauth_connected"] is True
        assert d["oauth_client_configured"] is True


class TestCreateOAuthConnectorTypes:
    @pytest.mark.parametrize("connector_type", ["google", "microsoft_graph", "salesforce"])
    def test_create_connector_accepts_oauth_types(self, client, connector_type):
        resp = client.post("/connectors/api/connectors", json={
            "name": f"my-{connector_type}",
            "connector_type": connector_type,
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["connector_type"] == connector_type
        Connector.query.filter_by(id=data["id"]).delete()
        db.session.commit()


class TestConnectorTestRoute:
    def test_no_client_credentials_configured(self, client, google_connector):
        resp = client.post(f"/connectors/api/connectors/{google_connector}/test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert "client_id" in data["message"]

    def test_credentials_but_not_connected(self, client, google_connector):
        c = Connector.query.get(google_connector)
        c.set_auth({"client_id": "cid", "client_secret": "secret"})
        db.session.commit()
        resp = client.post(f"/connectors/api/connectors/{google_connector}/test")
        data = resp.get_json()
        assert data["ok"] is False
        assert "not connected" in data["message"].lower()

    def test_connected_and_token_valid(self, client, google_connector, monkeypatch):
        from datetime import datetime, timedelta, timezone
        c = Connector.query.get(google_connector)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        c.set_auth({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "tok", "refresh_token": "r", "expires_at": future,
        })
        db.session.commit()
        resp = client.post(f"/connectors/api/connectors/{google_connector}/test")
        data = resp.get_json()
        assert data["ok"] is True

    def test_reauth_required_surfaces_as_not_ok(self, client, google_connector, monkeypatch):
        c = Connector.query.get(google_connector)
        c.set_auth({
            "client_id": "cid", "client_secret": "secret",
            "access_token": "stale", "refresh_token": "dead-refresh",
        })
        db.session.commit()

        def fake_refresh(*a, **k):
            raise oauth_mod.ReAuthRequired("dead")
        monkeypatch.setattr(oauth_mod, "refresh_access_token", fake_refresh)

        resp = client.post(f"/connectors/api/connectors/{google_connector}/test")
        data = resp.get_json()
        assert data["ok"] is False


class TestOAuthStartRoute:
    def test_requires_client_credentials(self, client, google_connector):
        resp = client.post(f"/connectors/api/connectors/{google_connector}/oauth/start")
        assert resp.status_code == 400
        assert "client_id" in resp.get_json()["error"]

    def test_rejects_non_oauth_connector(self, client):
        c = Connector(name="not-oauth", connector_type="github", config="{}")
        c.set_auth({"pat": "x"})
        db.session.add(c)
        db.session.commit()
        cid = c.id
        try:
            resp = client.post(f"/connectors/api/connectors/{cid}/oauth/start")
            assert resp.status_code == 400
        finally:
            Connector.query.filter_by(id=cid).delete()
            db.session.commit()

    def test_starts_flow_and_returns_flow_id(self, client, google_connector, monkeypatch):
        c = Connector.query.get(google_connector)
        c.set_auth({"client_id": "cid", "client_secret": "secret"})
        db.session.commit()

        monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda url: None)
        resp = client.post(f"/connectors/api/connectors/{google_connector}/oauth/start")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "flow_id" in data
        assert "accounts.google.com" in data["authorize_url"]

        with oauth_mod._flows_lock:
            oauth_mod._flows.pop(data["flow_id"], None)


class TestOAuthStatusRoute:
    def test_requires_flow_id(self, client, google_connector):
        resp = client.get(f"/connectors/api/connectors/{google_connector}/oauth/status")
        assert resp.status_code == 400

    def test_unknown_flow_id(self, client, google_connector):
        resp = client.get(f"/connectors/api/connectors/{google_connector}/oauth/status?flow_id=nope")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "unknown"
