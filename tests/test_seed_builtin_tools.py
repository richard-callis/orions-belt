"""
Tests for launch._seed_builtin_tools — in particular that it RECONCILES an
existing builtin tool's tier to match the current code on every call, not
just once at row creation. Bumping a tool's tier in code (e.g.
post_teams_message/create_salesforce_record 1 -> 2) is silently a no-op on
any install that already booted with the old tier unless the seeder
actively patches it back on startup.
"""
from app import db
from app.models.mcp_tool import MCPTool
from launch import _seed_builtin_tools


class TestSeedBuiltinTools:
    def test_creates_missing_tools_with_correct_tier(self, app):
        with app.app_context():
            MCPTool.query.filter_by(name="create_salesforce_record").delete()
            db.session.commit()
            try:
                _seed_builtin_tools(app)
                row = MCPTool.query.filter_by(name="create_salesforce_record").first()
                assert row is not None
                assert row.tier == 2
            finally:
                pass  # leave it seeded — every other test expects it to exist

    def test_reconciles_stale_tier_on_existing_builtin_row(self, app):
        """Simulates an install that seeded post_teams_message back when it
        was Tier 1 — the row already exists with the old tier, and a plain
        re-seed must correct it, not leave it frozen."""
        with app.app_context():
            row = MCPTool.query.filter_by(name="post_teams_message").first()
            assert row is not None, "expected post_teams_message to already be seeded"
            row.tier = 1  # simulate the stale pre-fix row
            db.session.commit()
            try:
                _seed_builtin_tools(app)
                reloaded = MCPTool.query.filter_by(name="post_teams_message").first()
                assert reloaded.tier == 2
            finally:
                # Restore to the correct tier regardless of outcome, so a
                # failure here doesn't leave other tests' tier assumptions
                # (e.g. tests that call run_tool_sync against this tool)
                # pointed at a stale value.
                reloaded = MCPTool.query.filter_by(name="post_teams_message").first()
                if reloaded and reloaded.tier != 2:
                    reloaded.tier = 2
                    db.session.commit()

    def test_comment_on_github_pr_is_tier_2(self, app):
        """comment_on_github_pr posts a visible, attributed comment on a PR
        other people see — the same class of effect as post_teams_message/
        send_email (Tier 2 each), not a private Tier-1 create. Simulates an
        install that seeded it back when it was Tier 1."""
        with app.app_context():
            row = MCPTool.query.filter_by(name="comment_on_github_pr").first()
            assert row is not None, "expected comment_on_github_pr to already be seeded"
            row.tier = 1  # simulate the stale pre-fix row
            db.session.commit()
            try:
                _seed_builtin_tools(app)
                reloaded = MCPTool.query.filter_by(name="comment_on_github_pr").first()
                assert reloaded.tier == 2
            finally:
                reloaded = MCPTool.query.filter_by(name="comment_on_github_pr").first()
                if reloaded and reloaded.tier != 2:
                    reloaded.tier = 2
                    db.session.commit()

    def test_never_touches_tier_of_a_non_builtin_row_with_the_same_name(self, app):
        """A user-created (non-builtin, e.g. Nova-sourced) tool happening to
        share a name with a builtin one must not have its tier silently
        overwritten by the reconciler — only source="builtin" rows are
        code-owned."""
        with app.app_context():
            row = MCPTool.query.filter_by(name="create_salesforce_record").first()
            assert row is not None
            original_source = row.source
            row.source = "nova"
            row.tier = 0
            db.session.commit()
            try:
                _seed_builtin_tools(app)
                reloaded = MCPTool.query.filter_by(name="create_salesforce_record").first()
                assert reloaded.tier == 0, "a non-builtin row's tier must not be reconciled"
            finally:
                reloaded = MCPTool.query.filter_by(name="create_salesforce_record").first()
                if reloaded:
                    reloaded.source = original_source
                    reloaded.tier = 2
                    db.session.commit()

    def test_idempotent_on_repeat_calls(self, app):
        with app.app_context():
            _seed_builtin_tools(app)
            before = MCPTool.query.filter_by(name="read_file").count()
            _seed_builtin_tools(app)
            after = MCPTool.query.filter_by(name="read_file").count()
            assert before == after == 1
