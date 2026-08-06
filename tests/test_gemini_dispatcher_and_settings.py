"""
Tests for Gemini routing in app/services/llm_adapters/dispatcher.py and the
provider-config plumbing in app/routes/settings.py that lets a Gemini/Vertex
provider actually be configured and safely persisted.
"""
import json

import pytest

from app.services.llm_adapters.dispatcher import get_adapter
from app.services.llm_adapters.gemini_adapter import GeminiAdapter
from app.services.llm_adapters.openai_adapter import OpenAIAdapter
from app.routes.settings import _looks_plaintext, _reencrypt_plaintext_keys

FAKE_SA_JSON = json.dumps({"type": "service_account", "project_id": "p"})


# ── Dispatcher routing ────────────────────────────────────────────────────────

class TestDispatcherGeminiRouting:
    def test_vertex_url_routes_to_gemini_adapter(self):
        adapter = get_adapter(
            "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1",
            FAKE_SA_JSON, "gemini-2.5-pro",
        )
        assert isinstance(adapter, GeminiAdapter)

    def test_ai_studio_url_routes_to_gemini_adapter(self):
        adapter = get_adapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
        assert isinstance(adapter, GeminiAdapter)

    def test_extra_config_passed_through_to_adapter(self):
        adapter = get_adapter(
            "https://us-central1-aiplatform.googleapis.com/...", FAKE_SA_JSON, "gemini-2.5-pro",
            extra={"project_id": "my-proj", "location": "europe-west4"},
        )
        assert adapter.project_id == "my-proj"
        assert adapter.location == "europe-west4"

    def test_other_urls_still_route_to_openai_adapter(self):
        """Regression guard: adding the Gemini branch must not shadow the
        existing generic OpenAI-compat fallback for everything else."""
        adapter = get_adapter("https://api.openai.com/v1", "sk-fake", "gpt-4o")
        assert isinstance(adapter, OpenAIAdapter)

    def test_missing_extra_defaults_to_none_without_raising(self):
        adapter = get_adapter("https://generativelanguage.googleapis.com", "AIzaFAKE", "gemini-2.0-flash")
        assert adapter.project_id is None


# ── _looks_plaintext / _reencrypt_plaintext_keys — JSON credential detection ──

class TestLooksPlaintextForServiceAccountJson:
    def test_json_blob_is_treated_as_plaintext(self):
        assert _looks_plaintext(FAKE_SA_JSON) is True

    def test_json_blob_with_leading_whitespace_is_treated_as_plaintext(self):
        assert _looks_plaintext("  " + FAKE_SA_JSON) is True

    def test_existing_prefix_detection_still_works(self):
        assert _looks_plaintext("sk-abc123") is True

    def test_fernet_shaped_token_is_not_plaintext(self):
        # Fernet tokens are opaque base64 — never start with '{' and never
        # match a known plaintext prefix.
        fake_fernet = "gAAAAABkX1Y2Z3" + "a" * 60
        assert _looks_plaintext(fake_fernet) is False


class TestReencryptPlaintextKeysForServiceAccountJson:
    def test_json_blob_gets_encrypted(self):
        providers = [{"id": "1", "api_key": FAKE_SA_JSON}]
        out = _reencrypt_plaintext_keys(providers)
        assert out[0]["api_key"] != FAKE_SA_JSON
        assert not out[0]["api_key"].strip().startswith("{")

    def test_masked_key_is_left_alone(self):
        providers = [{"id": "1", "api_key": "****1234"}]
        out = _reencrypt_plaintext_keys(providers)
        assert out[0]["api_key"] == "****1234"


# ── Provider CRUD routes — project_id/location persistence ───────────────────

class TestProviderCrudGeminiFields:
    def test_add_provider_persists_project_id_and_location(self, client):
        resp = client.post("/api/llm/providers", json={
            "name": "Enterprise Gemini", "type": "gemini",
            "base_url": "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1",
            "api_key": FAKE_SA_JSON, "model": "gemini-2.5-pro",
            "project_id": "my-proj", "location": "us-central1",
        })
        assert resp.status_code == 201
        provider_id = resp.get_json()["provider"]["id"]
        try:
            list_resp = client.get("/api/llm/providers")
            saved = next(p for p in list_resp.get_json()["providers"] if p["id"] == provider_id)
            assert saved["project_id"] == "my-proj"
            assert saved["location"] == "us-central1"
            # api_key must never come back in plaintext
            assert not saved["api_key"].strip().startswith("{")
        finally:
            client.delete(f"/api/llm/providers/{provider_id}")

    def test_add_provider_encrypts_service_account_json_at_rest(self, client, app):
        resp = client.post("/api/llm/providers", json={
            "name": "Enterprise Gemini 2", "type": "gemini",
            "base_url": "https://us-central1-aiplatform.googleapis.com/...",
            "api_key": FAKE_SA_JSON, "model": "gemini-2.5-pro",
            "project_id": "my-proj", "location": "us-central1",
        })
        provider_id = resp.get_json()["provider"]["id"]
        try:
            with app.app_context():
                from app.models.settings import Setting
                raw = Setting.get("llm.providers")  # value_type="json" — already a parsed list
                stored = next(p for p in raw if p["id"] == provider_id)
                assert not stored["api_key"].strip().startswith("{")
                assert stored["api_key"].startswith("gAAAAA")  # Fernet token signature
        finally:
            client.delete(f"/api/llm/providers/{provider_id}")

    def test_update_provider_can_change_project_id_and_location(self, client):
        resp = client.post("/api/llm/providers", json={
            "name": "Enterprise Gemini 3", "type": "gemini",
            "base_url": "https://us-central1-aiplatform.googleapis.com/...",
            "api_key": FAKE_SA_JSON, "model": "gemini-2.5-pro",
            "project_id": "old-proj", "location": "us-central1",
        })
        provider_id = resp.get_json()["provider"]["id"]
        try:
            patch_resp = client.patch(f"/api/llm/providers/{provider_id}", json={
                "project_id": "new-proj", "location": "europe-west4",
            })
            assert patch_resp.status_code == 200
            list_resp = client.get("/api/llm/providers")
            saved = next(p for p in list_resp.get_json()["providers"] if p["id"] == provider_id)
            assert saved["project_id"] == "new-proj"
            assert saved["location"] == "europe-west4"
        finally:
            client.delete(f"/api/llm/providers/{provider_id}")


# ── /api/llm/test — Gemini goes through the adapter, not the httpx probe ─────

class TestLlmTestRouteForGemini:
    def test_gemini_url_uses_adapter_not_httpx_probe(self, client, monkeypatch):
        """The generic probe posts to {base_url}/chat/completions, which
        Gemini's native API doesn't serve at all — regression guard that a
        Gemini test request goes through get_adapter()/complete() instead."""
        import app.services.llm_adapters as adapters_pkg

        called = {}

        class FakeAdapter:
            def complete(self, messages, tool_defs):
                called["messages"] = messages
                return ("ok", [], 5)

        def fake_get_adapter(base_url, api_key, model, extra=None):
            called["base_url"] = base_url
            called["extra"] = extra
            return FakeAdapter()

        # settings.py imports get_adapter locally inside the route function,
        # so patching the package's exported name is what that fresh import
        # picks up at call time.
        monkeypatch.setattr(adapters_pkg, "get_adapter", fake_get_adapter)

        resp = client.post("/api/llm/test", json={
            "base_url": "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1",
            "model": "gemini-2.5-pro", "api_key": FAKE_SA_JSON,
            "project_id": "p", "location": "us-central1",
        })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert called.get("base_url", "").startswith("https://us-central1-aiplatform")
        assert called["extra"] == {"project_id": "p", "location": "us-central1"}

    def test_gemini_transient_error_returns_503(self, client, monkeypatch):
        from app.services.llm import TransientError
        import app.services.llm_adapters as adapters_pkg

        class FailingAdapter:
            def complete(self, messages, tool_defs):
                raise TransientError("rate limited")

        monkeypatch.setattr(adapters_pkg, "get_adapter",
                             lambda *a, **k: FailingAdapter())

        resp = client.post("/api/llm/test", json={
            "base_url": "https://generativelanguage.googleapis.com",
            "model": "gemini-2.0-flash", "api_key": "AIzaFAKE",
        })
        assert resp.status_code == 503
