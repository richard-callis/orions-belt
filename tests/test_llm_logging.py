"""
Tests for the LLMLog choke-point recording added to _call_llm_sync, and the
per-adapter last_usage split (input/output tokens) each adapter now sets —
including the Ollama fix for a real bug: only eval_count (output tokens) was
ever counted, silently dropping the entire prompt/input side.
"""
import sys
import types
from types import SimpleNamespace

import pytest

from app import db
from app.models.logs import LLMLog
from app.models.settings import Setting
import app.services.llm as llm_mod


class TestEstimateLlmCost:
    def test_no_pricing_configured_returns_none(self, app):
        with app.app_context():
            assert llm_mod._estimate_llm_cost_and_savings("gpt-4o", 1000, 500) == (None, None)

    def test_unknown_model_returns_none(self, app):
        with app.app_context():
            Setting.set("llm.model_pricing", '{"claude-sonnet-5": {"input_per_1m": 3, "output_per_1m": 15}}')
            db.session.commit()
            try:
                assert llm_mod._estimate_llm_cost_and_savings("some-other-model", 1000, 500) == (None, None)
            finally:
                Setting.set("llm.model_pricing", "")
                db.session.commit()

    def test_computes_blended_cost_from_configured_pricing(self, app):
        with app.app_context():
            Setting.set("llm.model_pricing", '{"claude-sonnet-5": {"input_per_1m": 3.0, "output_per_1m": 15.0}}')
            db.session.commit()
            try:
                cost, savings = llm_mod._estimate_llm_cost_and_savings("claude-sonnet-5", 1_000_000, 1_000_000)
                assert cost == pytest.approx(18.0)
                assert savings is None
            finally:
                Setting.set("llm.model_pricing", "")
                db.session.commit()

    def test_self_hosted_model_reports_savings_not_cost(self, app):
        with app.app_context():
            Setting.set("llm.model_pricing", '{"llama3": {"input_per_1m": 1.0, "output_per_1m": 2.0, "self_hosted": true}}')
            db.session.commit()
            try:
                cost, savings = llm_mod._estimate_llm_cost_and_savings("llama3", 1_000_000, 1_000_000)
                assert cost is None
                assert savings == pytest.approx(3.0)
            finally:
                Setting.set("llm.model_pricing", "")
                db.session.commit()

    def test_malformed_pricing_json_fails_safe(self, app):
        with app.app_context():
            Setting.set("llm.model_pricing", "not json")
            db.session.commit()
            try:
                assert llm_mod._estimate_llm_cost_and_savings("gpt-4o", 1000, 500) == (None, None)
            finally:
                Setting.set("llm.model_pricing", "")
                db.session.commit()


class TestLogLlmCall:
    def test_writes_llmlog_row_on_success(self, app):
        with app.app_context():
            adapter = SimpleNamespace(last_usage={"input": 100, "output": 50, "model": "m"})
            before = LLMLog.query.count()
            llm_mod._log_llm_call(adapter, "test-model", "sess-1", "run-1", 250, success=True)
            rows = LLMLog.query.order_by(LLMLog.created_at.desc()).all()
            assert len(rows) == before + 1
            row = rows[0]
            try:
                assert row.tokens_in == 100
                assert row.tokens_out == 50
                assert row.latency_ms == 250
                assert row.success is True
                assert row.session_id == "sess-1"
                assert row.run_id == "run-1"
            finally:
                LLMLog.query.filter_by(id=row.id).delete()
                db.session.commit()

    def test_writes_llmlog_row_on_failure_with_error(self, app):
        with app.app_context():
            adapter = SimpleNamespace(last_usage=None)
            llm_mod._log_llm_call(adapter, "test-model", None, None, 100, success=False, error="boom")
            row = LLMLog.query.filter_by(success=False, error="boom").first()
            assert row is not None
            try:
                assert row.tokens_in == 0
                assert row.tokens_out == 0
            finally:
                LLMLog.query.filter_by(id=row.id).delete()
                db.session.commit()

    def test_never_raises_even_if_db_write_fails(self, app, monkeypatch):
        with app.app_context():
            adapter = SimpleNamespace(last_usage={"input": 1, "output": 1})

            def broken_commit():
                raise RuntimeError("db is down")

            monkeypatch.setattr(db.session, "commit", broken_commit)
            # Must not raise — logging failures can't break the LLM call it's observing.
            llm_mod._log_llm_call(adapter, "m", None, None, 1, success=True)

    def test_rolls_back_session_after_failed_commit(self, app, monkeypatch):
        """A failed commit must roll back the session, not just have its
        error swallowed — otherwise the shared scoped session is left with
        a pending failed transaction, and every later query in the same
        request/thread raises PendingRollbackError even though nothing
        about the actual LLM call or the caller's own work was at fault."""
        with app.app_context():
            adapter = SimpleNamespace(last_usage={"input": 1, "output": 1})

            def broken_commit():
                raise RuntimeError("db is down")

            rollback_calls = {"n": 0}
            real_rollback = db.session.rollback

            def spy_rollback():
                rollback_calls["n"] += 1
                return real_rollback()

            monkeypatch.setattr(db.session, "commit", broken_commit)
            monkeypatch.setattr(db.session, "rollback", spy_rollback)
            llm_mod._log_llm_call(adapter, "m", None, None, 1, success=True)
            assert rollback_calls["n"] == 1


class TestCallLlmSyncLogging:
    def test_logs_success_via_choke_point(self, app, monkeypatch):
        fake_adapter = SimpleNamespace(
            last_usage={"input": 10, "output": 5, "model": "m"},
            complete=lambda messages, tool_defs: ("hi", [], 15),
        )
        monkeypatch.setattr("app.services.llm_adapters.get_adapter", lambda *a, **k: fake_adapter)
        with app.app_context():
            before = LLMLog.query.count()
            result = llm_mod._call_llm_sync("http://x", "key", "m", [], [], session_id="s1", run_id="r1")
            assert result == ("hi", [], 15)
            assert LLMLog.query.count() == before + 1
            row = LLMLog.query.order_by(LLMLog.created_at.desc()).first()
            try:
                assert row.tokens_in == 10 and row.tokens_out == 5
                assert row.session_id == "s1" and row.run_id == "r1"
            finally:
                LLMLog.query.filter_by(id=row.id).delete()
                db.session.commit()

    def test_logs_failure_and_still_raises(self, app, monkeypatch):
        def broken_complete(messages, tool_defs):
            raise RuntimeError("provider down")

        fake_adapter = SimpleNamespace(last_usage=None, complete=broken_complete)
        monkeypatch.setattr("app.services.llm_adapters.get_adapter", lambda *a, **k: fake_adapter)
        with app.app_context():
            with pytest.raises(RuntimeError, match="provider down"):
                llm_mod._call_llm_sync("http://x", "key", "m", [], [])
            row = LLMLog.query.filter_by(success=False).order_by(LLMLog.created_at.desc()).first()
            assert row is not None
            assert "provider down" in row.error
            LLMLog.query.filter_by(id=row.id).delete()
            db.session.commit()


class TestOllamaAdapterTokenSplit:
    def _install_fake_ollama(self, prompt_eval_count, eval_count):
        fake = types.ModuleType("ollama")

        class FakeResponseError(Exception):
            def __init__(self, error="", status_code=500):
                self.error = error
                self.status_code = status_code
                super().__init__(error)

        class FakeMessage:
            content = "hello"
            tool_calls = []

        class FakeResp:
            message = FakeMessage()

        resp = FakeResp()
        resp.prompt_eval_count = prompt_eval_count
        resp.eval_count = eval_count

        class FakeClient:
            def __init__(self, host=None, timeout=None):
                pass

            def chat(self, **kwargs):
                return resp

        fake.Client = FakeClient
        fake.ResponseError = FakeResponseError
        sys.modules["ollama"] = fake
        return fake

    def test_last_usage_splits_input_and_output(self, app, monkeypatch):
        self._install_fake_ollama(prompt_eval_count=42, eval_count=8)
        try:
            from app.services.llm_adapters.ollama_adapter import OllamaAdapter
            adapter = OllamaAdapter("http://localhost:11434", "", "llama3")
            text, tool_calls, tokens = adapter.complete([], [])
            assert adapter.last_usage == {"input": 42, "output": 8, "model": "llama3"}
            assert tokens == 50   # combined, for backward-compat callers
        finally:
            sys.modules.pop("ollama", None)

    def test_previously_dropped_prompt_tokens_now_counted(self, app):
        # Regression: the old code only read eval_count (output), so a call
        # with a huge prompt and a tiny reply reported almost no usage at all.
        self._install_fake_ollama(prompt_eval_count=5000, eval_count=3)
        try:
            from app.services.llm_adapters.ollama_adapter import OllamaAdapter
            adapter = OllamaAdapter("http://localhost:11434", "", "llama3")
            _, _, tokens = adapter.complete([], [])
            assert tokens == 5003
        finally:
            sys.modules.pop("ollama", None)
