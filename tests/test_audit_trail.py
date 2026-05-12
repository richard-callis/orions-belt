"""Tests for audit trail session_id/run_id propagation."""
import os
from unittest.mock import MagicMock, patch

import pytest

from app import create_app, db
from app.models.logs import AuditLog
from app.services.backup import _verify_backup


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
def client(app):
    """Test client with auth fixture."""
    with app.test_client() as client:
        yield client


@pytest.fixture
def authenticated_user(app):
    """Create a test user and return their info."""
    from app.models.auth import User
    from app.services.auth import _hash_token

    token = "test-auth-token-" + os.urandom(16).hex()
    user = User(
        windows_username="TEST\\User",
        token_hash=_hash_token(token),
    )
    with app.app_context():
        db.session.add(user)
        db.session.commit()

    return {"token": token, "windows_username": "TEST\\User"}


@pytest.fixture
def auth_cookie(client, authenticated_user):
    """Set the auth cookie and return it."""
    token = authenticated_user["token"]
    client.set_cookie("orions_belt", "auth_token", token)
    return token


# ── Audit trail: session_id and run_id propagation ────────────────────────────


class TestAuditSessionIdPropagation:
    """Verify that session_id and run_id flow through to _log_audit."""

    def test_log_audit_receives_session_id(self, app):
        """_log_audit should record the session_id passed to execute_tool."""
        from app.services.mcp.tools import _log_audit

        with app.app_context():
            _log_audit(
                tool_name="read_file",
                tier=0,
                caller="TEST\\User",
                session_id="sess-abc123",
                run_id=None,
                input_params='{"path": "/tmp/test.txt"}',
                result="file contents",
            )

        with app.app_context():
            entry = AuditLog.query.filter_by(
                session_id="sess-abc123", tool_name="read_file"
            ).first()
            assert entry is not None
            assert entry.session_id == "sess-abc123"

    def test_log_audit_receives_run_id(self, app):
        """_log_audit should record the run_id passed to execute_tool."""
        from app.services.mcp.tools import _log_audit

        with app.app_context():
            _log_audit(
                tool_name="delete_file",
                tier=3,
                caller="TEST\\User",
                session_id=None,
                run_id="run-def456",
                input_params='{"path": "/tmp/test.txt"}',
                result="deleted",
            )

        with app.app_context():
            entry = AuditLog.query.filter_by(
                run_id="run-def456", tool_name="delete_file"
            ).first()
            assert entry is not None
            assert entry.run_id == "run-def456"

    def test_log_audit_both_ids(self, app):
        """_log_audit should record both session_id and run_id."""
        from app.services.mcp.tools import _log_audit

        with app.app_context():
            _log_audit(
                tool_name="modify_file",
                tier=2,
                caller="TEST\\User",
                session_id="sess-xyz",
                run_id="run-xyz",
                input_params='{"path": "/tmp/test.txt", "content": "new"}',
                result="modified",
            )

        with app.app_context():
            entry = AuditLog.query.filter_by(
                session_id="sess-xyz", run_id="run-xyz"
            ).first()
            assert entry is not None
            assert entry.tool_name == "modify_file"

    def test_log_audit_null_ids(self, app):
        """_log_audit should work with None for both session_id and run_id."""
        from app.services.mcp.tools import _log_audit

        with app.app_context():
            _log_audit(
                tool_name="list_directory",
                tier=0,
                caller="TEST\\User",
                session_id=None,
                run_id=None,
                input_params='{}',
                result="['file1.txt']",
            )

        with app.app_context():
            entries = AuditLog.query.filter_by(tool_name="list_directory").all()
            assert len(entries) == 1
            assert entries[0].session_id is None
            assert entries[0].run_id is None

    def test_input_summary_contains_input_params_not_result(self, app):
        """_log_audit should store input_params in input_summary, not result."""
        from app.services.mcp.tools import _log_audit

        input_params = '{"path": "/tmp/target.txt", "content": "sensitive-data-123"}'
        result = "file contents with different data"

        with app.app_context():
            _log_audit(
                tool_name="write_file",
                tier=1,
                caller="TEST\\User",
                session_id=None,
                run_id=None,
                input_params=input_params,
                result=result,
            )

        with app.app_context():
            entry = AuditLog.query.filter_by(tool_name="write_file").first()
            assert entry is not None
            # input_summary should contain the params, not the result
            assert "target.txt" in entry.input_summary
            assert "sensitive-data-123" in entry.input_summary
            # result_summary should contain the result
            assert "file contents" in entry.result_summary


class TestExecuteToolSetsGContext:
    """Verify execute_tool sets session_id/run_id on Flask g context."""

    def test_execute_tool_sets_session_id_on_g(self, app):
        """execute_tool should set g.orions_belt_session_id."""
        from flask import g

        with app.app_context():
            g.orions_belt_session_id = "g-test-123"
            assert getattr(g, "orions_belt_session_id", None) == "g-test-123"
            assert getattr(g, "orions_belt_run_id", None) is None

    def test_execute_tool_sets_run_id_on_g(self, app):
        """execute_tool should set g.orions_belt_run_id."""
        from flask import g

        with app.app_context():
            g.orions_belt_run_id = "g-run-456"
            assert getattr(g, "orions_belt_run_id", None) == "g-run-456"
            assert getattr(g, "orions_belt_session_id", None) is None


class TestAuditLogModel:
    """Verify the AuditLog model supports session_id and run_id."""

    def test_audit_log_has_session_id_column(self, app):
        """AuditLog should have a session_id column."""
        with app.app_context():
            entry = AuditLog(
                tool_name="test",
                tier=0,
                caller="test",
                session_id="test-session",
                run_id=None,
                outcome="auto",
                result_summary="ok",
            )
            db.session.add(entry)
            db.session.commit()

            result = AuditLog.query.filter_by(session_id="test-session").first()
            assert result is not None
            assert result.session_id == "test-session"

    def test_audit_log_has_run_id_column(self, app):
        """AuditLog should have a run_id column."""
        with app.app_context():
            entry = AuditLog(
                tool_name="test",
                tier=0,
                caller="test",
                session_id=None,
                run_id="test-run",
                outcome="auto",
                result_summary="ok",
            )
            db.session.add(entry)
            db.session.commit()

            result = AuditLog.query.filter_by(run_id="test-run").first()
            assert result is not None
            assert result.run_id == "test-run"

    def test_audit_log_has_input_summary_column(self, app):
        """AuditLog should have an input_summary column for tool args."""
        with app.app_context():
            entry = AuditLog(
                tool_name="test",
                tier=0,
                caller="test",
                session_id=None,
                run_id=None,
                outcome="auto",
                result_summary="ok",
                input_summary='{"path": "/tmp/test.txt"}',
            )
            db.session.add(entry)
            db.session.commit()

            result = AuditLog.query.filter_by(tool_name="test").first()
            assert result is not None
            assert "path" in result.input_summary
            assert "tmp" in result.input_summary
