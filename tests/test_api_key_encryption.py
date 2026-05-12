"""Tests for API key encryption in settings routes."""
import json
import os
import uuid

import pytest
from app import create_app, db
from app.models.settings import Setting


@pytest.fixture
def app():
    """Create app with test config and dropped/created tables."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"

    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app

    with app.app_context():
        db.drop_all()


@pytest.fixture
def client(app, monkeypatch):
    """Test client with auth bypassed for testing."""
    import app.auth as auth_mod
    monkeypatch.setattr(auth_mod, "check_auth", lambda: ("TEST\\TestUser", True))

    with app.test_client() as c:
        yield c


def _clear_providers(app):
    """Remove existing llm.providers so tests start clean."""
    with app.app_context():
        row = Setting.query.get("llm.providers")
        if row:
            db.session.delete(row)
            db.session.commit()
        db.session.remove()  # Clear session state


class TestApiKeyEncryption:
    def test_add_provider_encrypts_key(self, client, app):
        _clear_providers(app)
        resp = client.post("/api/llm/providers", json={
            "name": "TestEncrypt", "type": "genai",
            "base_url": "https://api.test.com/v1",
            "api_key": "sk-my-secret-key-12345",
            "model": "gpt-4",
        })
        assert resp.status_code == 201
        provider = resp.get_json()["provider"]
        assert provider["api_key"] != "sk-my-secret-key-12345"
        assert len(provider["api_key"]) > 50

    def test_get_providers_decrypts_key(self, app, client):
        _clear_providers(app)
        from app.routes.settings import _get_providers
        from app.services.crypto import encrypt_data

        encrypted = encrypt_data("sk-plaintext-secret")

        with app.app_context():
            row = Setting(
                key="llm.providers",
                value=json.dumps([
                    {"id": "test", "name": "TestDecrypt", "api_key": encrypted,
                     "type": "genai", "base_url": "https://test.com", "model": "gpt-4"}
                ]),
                value_type="json"
            )
            db.session.add(row)
            db.session.commit()

        with app.app_context():
            providers = _get_providers()
            assert len(providers) == 1
            assert providers[0]["api_key"] == "sk-plaintext-secret"

    def test_redact_providers_masks_key(self, app):
        """_redact_providers should mask API keys in API responses."""
        from app.routes.settings import _redact_providers

        providers = [
            {"id": "1", "name": "Test", "api_key": "sk-secret-key-12345",
             "type": "genai", "base_url": "https://test.com", "model": "gpt-4"}
        ]

        result = _redact_providers(providers)
        key = result[0]["api_key"]
        assert key != "sk-secret-key-12345"
        assert key[-4:] == "2345"

    def test_update_provider_encrypts_new_key(self, client, app):
        _clear_providers(app)
        resp = client.post("/api/llm/providers", json={
            "name": "TestUpdate", "type": "genai",
            "base_url": "https://test.com",
            "api_key": "sk-old-key-12345",
            "model": "gpt-4",
        })
        provider_id = resp.get_json()["provider"]["id"]

        resp = client.patch(f"/api/llm/providers/{provider_id}", json={
            "api_key": "sk-new-secret-key-67890",
        })
        assert resp.status_code == 200

        with app.app_context():
            row = Setting.query.get("llm.providers")
            providers = json.loads(row.value)
            key = next(p["api_key"] for p in providers if p["name"] == "TestUpdate")
            assert key != "sk-new-secret-key-67890"
            assert len(key) > 50

    def test_update_preserves_key_when_masked(self, client, app):
        _clear_providers(app)
        # Verify state before POST
        with app.app_context():
            row = Setting.query.get("llm.providers")
            print(f"BEFORE POST: providers={row.value[:200] if row and row.value else 'None'}")
        resp = client.post("/api/llm/providers", json={
            "name": "TestMasked", "type": "genai",
            "base_url": "https://test.com",
            "api_key": "sk-original-key-abcde",
            "model": "gpt-4",
        })
        provider_id = resp.get_json()["provider"]["id"]

        resp = client.patch(f"/api/llm/providers/{provider_id}", json={
            "api_key": "*********abcde",
        })
        assert resp.status_code == 200

        with app.app_context():
            row = Setting.query.get("llm.providers")
            providers = json.loads(row.value)
            key = next(p["api_key"] for p in providers if p["name"] == "TestMasked")
            assert key != "*********abcde"
            assert len(key) > 50
