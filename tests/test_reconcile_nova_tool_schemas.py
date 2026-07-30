"""
Tests for launch._reconcile_nova_tool_schemas — the migration that updates
tier/schema for the four Nova-sourced MCPTool rows (run_python, run_shell,
fetch_url, http_request) whose published contract changed after they first
shipped. _seed_builtin_tools only reconciles source="builtin" rows, so
without this, a tier correction (e.g. run_python 2 -> 3) would silently
never apply on any install where the Nova was already activated.
"""
from app import db
from app.models.mcp_tool import MCPTool
from launch import _reconcile_nova_tool_schemas


class TestReconcileNovaToolSchemas:
    def test_updates_tier_of_existing_nova_sourced_row(self, app):
        with app.app_context():
            row = MCPTool(id="t-nova1", name="run_python", tier=2, enabled=True,
                          source="nova", description="old", input_schema="{}")
            db.session.add(row)
            db.session.commit()
            try:
                _reconcile_nova_tool_schemas(app)
                reloaded = MCPTool.query.filter_by(id="t-nova1").first()
                assert reloaded.tier == 3
                assert "not sandboxed" in reloaded.description.lower()
            finally:
                MCPTool.query.filter_by(id="t-nova1").delete()
                db.session.commit()

    def test_never_creates_a_row_that_does_not_exist(self, app):
        with app.app_context():
            MCPTool.query.filter_by(name="run_python", source="nova").delete()
            db.session.commit()
            _reconcile_nova_tool_schemas(app)
            assert MCPTool.query.filter_by(name="run_python", source="nova").first() is None

    def test_never_touches_a_builtin_sourced_row_of_the_same_name(self, app):
        # Defensive: these four names are only ever meant to be nova-sourced,
        # but the reconcile must not silently rewrite an unrelated row.
        with app.app_context():
            row = MCPTool(id="t-nova2", name="fetch_url", tier=0, enabled=True,
                          source="builtin", description="custom", input_schema="{}")
            db.session.add(row)
            db.session.commit()
            try:
                _reconcile_nova_tool_schemas(app)
                reloaded = MCPTool.query.filter_by(id="t-nova2").first()
                assert reloaded.tier == 0
                assert reloaded.description == "custom"
            finally:
                MCPTool.query.filter_by(id="t-nova2").delete()
                db.session.commit()

    def test_updates_http_request_and_fetch_url_schemas(self, app):
        with app.app_context():
            fetch_row = MCPTool(id="t-nova3", name="fetch_url", tier=1, enabled=True,
                                source="nova", description="old", input_schema='{"old": true}')
            http_row = MCPTool(id="t-nova4", name="http_request", tier=1, enabled=True,
                               source="nova", description="old", input_schema='{"old": true}')
            db.session.add_all([fetch_row, http_row])
            db.session.commit()
            try:
                _reconcile_nova_tool_schemas(app)
                fetch_reloaded = MCPTool.query.filter_by(id="t-nova3").first()
                http_reloaded = MCPTool.query.filter_by(id="t-nova4").first()
                assert "method" not in fetch_reloaded.input_schema  # trimmed schema
                assert fetch_reloaded.tier == 1
                assert http_reloaded.tier == 2  # bumped from 1
            finally:
                MCPTool.query.filter(MCPTool.id.in_(["t-nova3", "t-nova4"])).delete(synchronize_session=False)
                db.session.commit()

    def test_idempotent_on_repeat_calls(self, app):
        with app.app_context():
            row = MCPTool(id="t-nova5", name="run_shell", tier=3, enabled=True,
                          source="nova", description="old", input_schema="{}")
            db.session.add(row)
            db.session.commit()
            try:
                _reconcile_nova_tool_schemas(app)
                _reconcile_nova_tool_schemas(app)
                reloaded = MCPTool.query.filter_by(id="t-nova5").first()
                assert reloaded.tier == 3
            finally:
                MCPTool.query.filter_by(id="t-nova5").delete()
                db.session.commit()
