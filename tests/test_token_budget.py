"""
Tests for token budget enforcement, usage recording, and step checkpointing.
"""
import pytest
from app.services.agents import (
    _compute_checkpoint_hash,
    _check_token_budget,
    _record_token_usage,
)


class TestCheckpointHash:
    def test_deterministic(self):
        h1 = _compute_checkpoint_hash("read_file", {"path": "/tmp/foo.txt"})
        h2 = _compute_checkpoint_hash("read_file", {"path": "/tmp/foo.txt"})
        assert h1 == h2

    def test_different_tool_names(self):
        h1 = _compute_checkpoint_hash("read_file", {"path": "/tmp/x"})
        h2 = _compute_checkpoint_hash("write_file", {"path": "/tmp/x"})
        assert h1 != h2

    def test_different_args(self):
        h1 = _compute_checkpoint_hash("shell", {"cmd": "ls"})
        h2 = _compute_checkpoint_hash("shell", {"cmd": "pwd"})
        assert h1 != h2

    def test_arg_order_irrelevant(self):
        """Args are JSON-dumped with sort_keys=True so order doesn't matter."""
        h1 = _compute_checkpoint_hash("tool", {"b": 2, "a": 1})
        h2 = _compute_checkpoint_hash("tool", {"a": 1, "b": 2})
        assert h1 == h2

    def test_returns_64_char_hex(self):
        h = _compute_checkpoint_hash("tool", {})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestTokenBudget:
    def test_no_budget_always_passes(self, app, db):
        from app import db as _db
        from app.models.agent import Agent
        import uuid

        with app.app_context():
            agent = Agent(
                id=str(uuid.uuid4()),
                name="No Budget Agent",
                daily_token_budget=None,
                monthly_token_budget=None,
            )
            _db.session.add(agent)
            _db.session.flush()
            assert _check_token_budget(agent) is None

    def test_daily_budget_not_exceeded(self, app, db):
        from app import db as _db
        from app.models.agent import Agent
        import uuid

        with app.app_context():
            agent = Agent(
                id=str(uuid.uuid4()),
                name="Budgeted Agent",
                daily_token_budget=10000,
            )
            _db.session.add(agent)
            _db.session.flush()
            _record_token_usage(agent.id, None, 5000)
            assert _check_token_budget(agent) is None

    def test_daily_budget_exceeded(self, app, db):
        from app import db as _db
        from app.models.agent import Agent
        import uuid

        with app.app_context():
            agent = Agent(
                id=str(uuid.uuid4()),
                name="Over Budget Agent",
                daily_token_budget=1000,
            )
            _db.session.add(agent)
            _db.session.flush()
            _record_token_usage(agent.id, None, 1500)
            err = _check_token_budget(agent)
            assert err is not None
            assert "Daily token budget exceeded" in err

    def test_monthly_budget_exceeded(self, app, db):
        from app import db as _db
        from app.models.agent import Agent
        import uuid

        with app.app_context():
            agent = Agent(
                id=str(uuid.uuid4()),
                name="Monthly Over Budget",
                monthly_token_budget=500,
            )
            _db.session.add(agent)
            _db.session.flush()
            _record_token_usage(agent.id, None, 600)
            err = _check_token_budget(agent)
            assert err is not None
            assert "Monthly token budget exceeded" in err

    def test_record_token_usage_stores_period(self, app, db):
        from app import db as _db
        from app.models.agent import Agent, TokenUsage
        from app.services.agents import _today, _this_month
        import uuid

        with app.app_context():
            agent = Agent(id=str(uuid.uuid4()), name="Usage Test Agent")
            _db.session.add(agent)
            _db.session.flush()

            _record_token_usage(agent.id, None, 42)
            row = TokenUsage.query.filter_by(agent_id=agent.id).first()
            assert row is not None
            assert row.tokens_used == 42
            assert row.period_day == _today()
            assert row.period_month == _this_month()
