"""
Tests for launch._seed_novas — in particular that it RECONCILES an existing
bundled Nova row's config (tier/schema/description) to match the current
code on every call, not just once at row creation.

Without this, a Nova row seeded by an older version of launch.py keeps its
STALE config forever (bundled Novas can't be edited via the API — nova.py's
PATCH/DELETE routes 403 on source=="bundled" — so there's no per-install
customization to preserve). Activating a bundled Nova (mcp_tool import,
app/routes/nova.py::_import_mcp_tool) later replays whatever tier/schema is
baked into that stale config, not the corrected current one — e.g. if
mcp_python_runner's run_python tier were ever corrected from 3 down/up in
code, an already-seeded Nova row wouldn't pick that up until this reconciles
it.
"""
import json

from app import db
from app.models.nova import Nova
from launch import _seed_novas


class TestSeedNovas:
    def test_creates_missing_bundled_novas(self, app):
        with app.app_context():
            Nova.query.filter_by(name="mcp_python_runner").delete()
            db.session.commit()
            try:
                _seed_novas(app)
                row = Nova.query.filter_by(name="mcp_python_runner").first()
                assert row is not None
                assert row.source == "bundled"
                cfg = json.loads(row.config)
                assert cfg["tools"][0]["tier"] == 3
            finally:
                pass  # leave it seeded — other tests may expect it to exist

    def test_reconciles_stale_config_on_existing_bundled_row(self, app):
        """Simulates a Nova row seeded by an older version of the code with a
        stale tier baked into its config — a plain re-seed must correct it,
        not leave it frozen (this is exactly what happens if a user upgrades
        and only THEN activates a bundled Nova)."""
        with app.app_context():
            row = Nova.query.filter_by(name="mcp_python_runner").first()
            assert row is not None, "expected mcp_python_runner to already be seeded"
            stale_config = {
                "tools": [
                    {
                        "name": "run_python",
                        "description": "stale description from an old version",
                        "tier": 0,  # simulate a corrected-since tier
                        "input_schema": {"type": "object", "properties": {}},
                    },
                ],
            }
            row.config = json.dumps(stale_config)
            row.description = "stale nova description"
            db.session.commit()
            try:
                _seed_novas(app)
                reloaded = Nova.query.filter_by(name="mcp_python_runner").first()
                cfg = json.loads(reloaded.config)
                assert cfg["tools"][0]["tier"] == 3
                assert cfg["tools"][0]["description"] != "stale description from an old version"
                assert reloaded.description != "stale nova description"
            finally:
                _seed_novas(app)  # restore correct state regardless of outcome

    def test_never_touches_a_user_sourced_nova(self, app):
        """A user-created Nova's config is theirs to keep — only
        source="bundled" rows are code-owned and safe to overwrite (bundled
        Novas can't even be edited via the API, so there's nothing of the
        user's to preserve there; the same isn't true for source="user")."""
        with app.app_context():
            nova = Nova(
                id="test-user-nova-1", name="mcp_python_runner_user_test",
                display_name="My Custom Tool", nova_type="mcp_tool",
                category="Code Execution", source="user",
                tags="[]", config=json.dumps({"tools": [{"name": "custom_tool", "tier": 0}]}),
                description="user's own description",
            )
            db.session.add(nova)
            db.session.commit()
            try:
                _seed_novas(app)
                reloaded = Nova.query.filter_by(name="mcp_python_runner_user_test").first()
                assert reloaded is not None
                assert reloaded.source == "user"
                assert reloaded.description == "user's own description"
                cfg = json.loads(reloaded.config)
                assert cfg["tools"][0]["name"] == "custom_tool"
            finally:
                Nova.query.filter_by(id="test-user-nova-1").delete()
                db.session.commit()

    def test_idempotent_on_repeat_calls(self, app):
        with app.app_context():
            _seed_novas(app)
            before = Nova.query.filter_by(name="mcp_python_runner").count()
            _seed_novas(app)
            after = Nova.query.filter_by(name="mcp_python_runner").count()
            assert before == after == 1
