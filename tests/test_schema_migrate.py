"""
Tests for the schema auto-reconciler in launch._migrate_schema.

Regression: a DB created before a column was added to a model was missing that
column (create_all only adds tables, not columns), raising "no such column" at
runtime. The reconciler must ALTER-add any model column the DB lacks.
"""
from app import db
from launch import _migrate_schema, _column_add_ddl


class TestSchemaReconcile:
    def test_adds_missing_agent_columns(self, app):
        with app.app_context():
            db.session.remove()
            # Simulate an old DB: an agents table missing the newer columns.
            with db.engine.begin() as conn:
                conn.execute(db.text("DROP TABLE IF EXISTS agents"))
                conn.execute(db.text(
                    "CREATE TABLE agents ("
                    "  id VARCHAR(36) PRIMARY KEY,"
                    "  name VARCHAR(200) NOT NULL,"
                    "  created_at DATETIME,"
                    "  updated_at DATETIME)"
                ))
            try:
                _migrate_schema(app)
                with db.engine.connect() as conn:
                    cols = {r[1] for r in conn.execute(db.text("PRAGMA table_info(agents)"))}
                # The columns that were missing from the user's crash report:
                assert "daily_token_budget" in cols
                assert "monthly_token_budget" in cols
                assert "role_scope" in cols
                # ...and the rest of the model's columns.
                assert {"system_prompt", "allowed_tools", "max_iterations", "status"} <= cols
            finally:
                # Restore the full schema so other tests are unaffected.
                with db.engine.begin() as conn:
                    conn.execute(db.text("DROP TABLE IF EXISTS agents"))
                db.create_all()

    def test_adds_missing_feature_column(self, app):
        with app.app_context():
            db.session.remove()
            with db.engine.begin() as conn:
                conn.execute(db.text("DROP TABLE IF EXISTS features"))
                conn.execute(db.text(
                    "CREATE TABLE features ("
                    "  id VARCHAR(36) PRIMARY KEY,"
                    "  epic_id VARCHAR(36),"
                    "  title VARCHAR(300),"
                    "  created_at DATETIME)"
                ))
            try:
                _migrate_schema(app)
                with db.engine.connect() as conn:
                    cols = {r[1] for r in conn.execute(db.text("PRAGMA table_info(features)"))}
                assert "plan_approved_at" in cols  # the crash-report column
            finally:
                with db.engine.begin() as conn:
                    conn.execute(db.text("DROP TABLE IF EXISTS features"))
                db.create_all()

    def test_idempotent_on_full_schema(self, app):
        """Running against an up-to-date DB adds nothing and does not error."""
        with app.app_context():
            _migrate_schema(app)   # should be a no-op
            _migrate_schema(app)   # twice — still fine

    def test_column_add_ddl_types(self, app):
        with app.app_context():
            from app.models.agent import Agent
            cols = {c.name: c for c in Agent.__table__.columns}
            dialect = db.engine.dialect
            # nullable int budget → no NOT NULL, no default
            ddl = _column_add_ddl(cols["daily_token_budget"], dialect)
            assert ddl.startswith("daily_token_budget ")
            assert "NOT NULL" not in ddl
            # role_scope is a nullable string
            assert _column_add_ddl(cols["role_scope"], dialect).startswith("role_scope ")
