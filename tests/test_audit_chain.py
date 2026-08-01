"""
Tests for app/services/audit_chain.py — the AuditLog hash chain, wired into
_log_audit() in app/services/mcp/tools.py.

Uses its own isolated in-memory DB per test (same pattern as
test_audit_trail.py/test_retention.py) rather than the shared session-scoped
conftest DB — verify_chain() walks the ENTIRE audit_logs table, so these
tests need a clean, private table to make exact assertions about row counts
and chain state without being at the mercy of what other test files have
already written to a shared table.
"""
import datetime as dt

import pytest

from app import create_app, db
from app.models.logs import AuditLog
from app.services.audit_chain import compute_row_hash, get_last_row_hash, verify_chain
from app.services.mcp import tools as mcp_tools


@pytest.fixture
def app():
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
    with app.test_client() as client:
        yield client


class TestComputeRowHash:
    def test_deterministic_for_same_inputs(self, app):
        with app.app_context():
            ts = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
            h1 = compute_row_hash(None, ts, "read_file", 0, "user", "s1", None, "{}", "auto", "ok", None)
            h2 = compute_row_hash(None, ts, "read_file", 0, "user", "s1", None, "{}", "auto", "ok", None)
            assert h1 == h2

    def test_changes_if_any_field_changes(self, app):
        with app.app_context():
            ts = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
            base = compute_row_hash(None, ts, "read_file", 0, "user", "s1", None, "{}", "auto", "ok", None)
            changed = compute_row_hash(None, ts, "read_file", 0, "user", "s1", None, "{}", "auto", "DIFFERENT", None)
            assert base != changed

    def test_changes_if_previous_hash_changes(self, app):
        with app.app_context():
            ts = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
            h1 = compute_row_hash("aaa", ts, "read_file", 0, "user", "s1", None, "{}", "auto", "ok", None)
            h2 = compute_row_hash("bbb", ts, "read_file", 0, "user", "s1", None, "{}", "auto", "ok", None)
            assert h1 != h2


class TestLogAuditWritesChain:
    def test_first_row_has_no_previous_hash(self, app):
        with app.app_context():
            mcp_tools._log_audit("read_file", 0, "user", None, None, "{}", "ok")
            row = AuditLog.query.order_by(AuditLog.created_at.desc()).first()
            assert row.previous_hash is None
            assert row.row_hash is not None

    def test_second_row_links_to_first(self, app):
        with app.app_context():
            mcp_tools._log_audit("read_file", 0, "user", None, None, "{}", "first")
            first = AuditLog.query.order_by(AuditLog.created_at.desc()).first()
            mcp_tools._log_audit("read_file", 0, "user", None, None, "{}", "second")
            second = AuditLog.query.filter(AuditLog.id != first.id).order_by(
                AuditLog.created_at.desc()).first()
            assert second.previous_hash == first.row_hash
            assert get_last_row_hash() == second.row_hash


class TestVerifyChain:
    def test_valid_chain_of_several_rows(self, app):
        with app.app_context():
            for i in range(5):
                mcp_tools._log_audit("read_file", 0, "user", None, None, "{}", f"result-{i}")
            result = verify_chain()
            assert result["valid"] is True
            assert result["chained_rows"] == 5
            assert result["first_break_row_id"] is None

    def test_detects_tampered_content(self, app):
        """Modifying a row's stored content after it was written must be
        detectable — the whole point of the chain."""
        with app.app_context():
            for i in range(3):
                mcp_tools._log_audit("read_file", 0, "user", None, None, "{}", f"result-{i}")

            rows = AuditLog.query.order_by(AuditLog.created_at.asc()).all()
            tampered = rows[1]
            tampered.result_summary = "TAMPERED CONTENT"
            db.session.commit()

            result = verify_chain()
            assert result["valid"] is False
            assert result["first_break_row_id"] == tampered.id
            assert "modified after write" in result["reason"]

    def test_detects_deleted_row(self, app):
        """Deleting a row from the middle of the chain breaks the link
        between its neighbors — must be detectable."""
        with app.app_context():
            for i in range(3):
                mcp_tools._log_audit("read_file", 0, "user", None, None, "{}", f"result-{i}")

            rows = AuditLog.query.order_by(AuditLog.created_at.asc()).all()
            AuditLog.query.filter_by(id=rows[1].id).delete()
            db.session.commit()

            result = verify_chain()
            assert result["valid"] is False
            assert result["first_break_row_id"] == rows[2].id

    def test_legacy_unchained_rows_do_not_break_verification(self, app):
        """Rows written before this feature existed have row_hash=NULL —
        those must be skipped, not treated as tampering, and a chained row
        written after them must correctly start a fresh chain (previous_hash
        None) rather than being flagged as broken."""
        with app.app_context():
            legacy = AuditLog(
                id="legacy-1", tool_name="read_file", tier=0, caller="user",
                outcome="auto", input_summary="{}", result_summary="legacy row",
                previous_hash=None, row_hash=None,
            )
            db.session.add(legacy)
            db.session.commit()

            mcp_tools._log_audit("read_file", 0, "user", None, None, "{}", "new chained row")

            result = verify_chain()
            assert result["valid"] is True
            assert result["chained_rows"] == 1
            assert result["total_rows"] == 2

    def test_empty_log_is_trivially_valid(self, app):
        with app.app_context():
            result = verify_chain()
            assert result["valid"] is True
            assert result["total_rows"] == 0
            assert result["chained_rows"] == 0


class TestAuditVerifyRoute:
    def test_route_returns_valid_json(self, app, client):
        with app.app_context():
            mcp_tools._log_audit("read_file", 0, "user", None, None, "{}", "ok")
        resp = client.get("/logs/api/logs/audit/verify")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is True
