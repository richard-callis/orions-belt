"""
Tests for the LLM usage/cost dashboard API routes.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.models.agent import Agent
from app.models.logs import LLMLog
from app.models.settings import Setting


class TestUsageSummary:
    def _make_log(self, **overrides):
        kwargs = dict(
            id=f"log-{overrides.get('model', 'm')}-{overrides.get('run_id', 'r')}",
            provider="anthropic", model="claude-sonnet-5",
            session_id="room-1", run_id="agent-1",
            tokens_in=100, tokens_out=50, latency_ms=200,
            estimated_cost_usd=0.01, success=True,
        )
        kwargs.update(overrides)
        return LLMLog(**kwargs)

    def test_totals_aggregate_across_logs(self, app, client):
        with app.app_context():
            db.session.add_all([
                self._make_log(id="l1", tokens_in=100, tokens_out=50, estimated_cost_usd=0.01),
                self._make_log(id="l2", tokens_in=200, tokens_out=75, estimated_cost_usd=0.02),
            ])
            db.session.commit()
        try:
            resp = client.get("/api/usage/summary?days=30")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["totals"]["tokens_in"] == 300
            assert data["totals"]["tokens_out"] == 125
            assert data["totals"]["calls"] == 2
            assert data["totals"]["cost_usd"] == pytest.approx(0.03)
        finally:
            with app.app_context():
                LLMLog.query.filter(LLMLog.id.in_(["l1", "l2"])).delete(synchronize_session=False)
                db.session.commit()

    def test_by_model_breakdown(self, app, client):
        with app.app_context():
            db.session.add_all([
                self._make_log(id="l3", model="claude-sonnet-5"),
                self._make_log(id="l4", model="gpt-4o"),
            ])
            db.session.commit()
        try:
            resp = client.get("/api/usage/summary?days=30")
            models = {m["model"] for m in resp.get_json()["by_model"]}
            assert {"claude-sonnet-5", "gpt-4o"} <= models
        finally:
            with app.app_context():
                LLMLog.query.filter(LLMLog.id.in_(["l3", "l4"])).delete(synchronize_session=False)
                db.session.commit()

    def test_self_hosted_badge_derives_from_recorded_savings_not_live_pricing(self, app, client):
        """Regression: self_hosted used to be a live lookup against current
        pricing config, independent of the cost/savings actually frozen on
        each row at write time. Flipping a model's pricing after historical
        rows were written would then show a "self-hosted" badge next to a
        model whose recorded $ is all real spend (or vice versa) — visibly
        contradictory data in the same response. Derive it from the
        aggregated cost_usd/savings_usd instead so the badge can never
        disagree with the numbers next to it."""
        with app.app_context():
            db.session.add(self._make_log(
                id="l-savings", model="local-llama", estimated_cost_usd=None, estimated_savings_usd=0.5,
            ))
            db.session.commit()
            # Pricing now says this model is NOT self-hosted — contradicts
            # the historical row, which should still win since it reflects
            # what was actually recorded.
            Setting.set("llm.model_pricing", '{"local-llama": {"input_per_1m": 3.0, "output_per_1m": 15.0, "self_hosted": false}}', value_type="string")
            db.session.commit()
        try:
            resp = client.get("/api/usage/summary?days=30")
            model = next(m for m in resp.get_json()["by_model"] if m["model"] == "local-llama")
            assert model["self_hosted"] is True
            assert model["cost_usd"] == 0.0
            assert model["savings_usd"] == pytest.approx(0.5)
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id="l-savings").delete()
                Setting.set("llm.model_pricing", "", value_type="string")
                db.session.commit()

    def test_by_agent_resolves_agent_name(self, app, client):
        with app.app_context():
            db.session.add(Agent(id="agent-usage-1", name="Project Planner", status="idle"))
            db.session.add(self._make_log(id="l5", run_id="agent-usage-1"))
            db.session.commit()
        try:
            resp = client.get("/api/usage/summary?days=30")
            by_agent = {a["agent_id"]: a["agent_name"] for a in resp.get_json()["by_agent"]}
            assert by_agent.get("agent-usage-1") == "Project Planner"
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id="l5").delete()
                Agent.query.filter_by(id="agent-usage-1").delete()
                db.session.commit()

    def test_unattributed_calls_grouped_together(self, app, client):
        with app.app_context():
            db.session.add(self._make_log(id="l6", run_id=None))
            db.session.commit()
        try:
            resp = client.get("/api/usage/summary?days=30")
            by_agent = {a["agent_id"] for a in resp.get_json()["by_agent"]}
            assert "(unattributed)" in by_agent
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id="l6").delete()
                db.session.commit()

    def test_excludes_logs_outside_window(self, app, client):
        with app.app_context():
            old = self._make_log(id="l7")
            old.created_at = datetime.now(timezone.utc) - timedelta(days=60)
            db.session.add(old)
            db.session.commit()
        try:
            resp = client.get("/api/usage/summary?days=7")
            data = resp.get_json()
            assert not any(a["agent_id"] == "agent-1" and a["calls"] > 0 for a in data["by_agent"]) or \
                   data["totals"]["calls"] == 0
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id="l7").delete()
                db.session.commit()

    def test_days_param_is_clamped(self, app, client):
        resp = client.get("/api/usage/summary?days=99999")
        assert resp.status_code == 200
        assert resp.get_json()["days"] == 365

    def test_todays_call_lands_in_last_day_bucket(self, app, client):
        """Regression: since = datetime.now() - timedelta(days=days) (not
        midnight-anchored) put the last zero-filled day-key at "now minus
        ~1 day" instead of today, so a call made right now fell outside
        every pre-filled bucket — most visibly, an agent's `last7`
        sparkline (built only from those pre-filled keys) showed all
        zeros even with fresh activity."""
        with app.app_context():
            db.session.add(self._make_log(id="l8", run_id="agent-today"))
            db.session.commit()
        try:
            resp = client.get("/api/usage/summary?days=30")
            data = resp.get_json()
            assert data["by_day"][-1]["tokens_in"] == 100
            agent = next(a for a in data["by_agent"] if a["agent_id"] == "agent-today")
            assert agent["last7"][-1] == 150  # tokens_in + tokens_out from _make_log
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id="l8").delete()
                db.session.commit()



class TestUsagePricing:
    def test_get_pricing_defaults_empty(self, app, client):
        with app.app_context():
            Setting.set("llm.model_pricing", "", value_type="string")
            db.session.commit()
        resp = client.get("/api/usage/pricing")
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_set_and_get_pricing_round_trips(self, app, client):
        payload = {"claude-sonnet-5": {"input_per_1m": 3.0, "output_per_1m": 15.0}}
        put_resp = client.put("/api/usage/pricing", json=payload)
        assert put_resp.status_code == 200
        try:
            get_resp = client.get("/api/usage/pricing")
            assert get_resp.get_json() == payload
        finally:
            with app.app_context():
                Setting.set("llm.model_pricing", "", value_type="string")
                db.session.commit()

    def test_set_pricing_rejects_non_object_body(self, app, client):
        resp = client.put("/api/usage/pricing", json=["not", "a", "dict"])
        assert resp.status_code == 400

    def test_set_pricing_rejects_non_numeric_price(self, app, client):
        resp = client.put("/api/usage/pricing", json={"m": {"input_per_1m": "free"}})
        assert resp.status_code == 400

    def test_pricing_feeds_estimate_llm_cost(self, app, client):
        import app.services.llm as llm_mod
        payload = {"test-model-xyz": {"input_per_1m": 2.0, "output_per_1m": 10.0}}
        client.put("/api/usage/pricing", json=payload)
        try:
            with app.app_context():
                cost, savings = llm_mod._estimate_llm_cost_and_savings("test-model-xyz", 1_000_000, 1_000_000)
                assert cost == 12.0
                assert savings is None
        finally:
            with app.app_context():
                Setting.set("llm.model_pricing", "", value_type="string")
                db.session.commit()

    def test_self_hosted_pricing_reports_savings_not_cost(self, app, client):
        import app.services.llm as llm_mod
        payload = {"local-llama": {"input_per_1m": 0.5, "output_per_1m": 1.0, "self_hosted": True}}
        client.put("/api/usage/pricing", json=payload)
        try:
            with app.app_context():
                cost, savings = llm_mod._estimate_llm_cost_and_savings("local-llama", 1_000_000, 1_000_000)
                assert cost is None
                assert savings == 1.5
        finally:
            with app.app_context():
                Setting.set("llm.model_pricing", "", value_type="string")
                db.session.commit()
