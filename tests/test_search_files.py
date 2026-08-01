"""
Tests for the search_files MCP tool (app/services/mcp/tools.py).

Regression coverage for a real path-traversal gap found while reviewing
REVIEW_EFFICIENCY_AND_SECURITY.md's findings against current code:
_authorize_path was only checked against the search ROOT, never against
each individual glob match — a pattern with ../ components could return
matches for files entirely outside the authorized directory. Verified
exploitable with a real Path.glob() call before writing the fix.
"""
import asyncio

import pytest

from app import db
from app.models.connector import AuthorizedDirectory
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def authorized_dir(app, tmp_path):
    d = tmp_path / "authorized"
    d.mkdir()
    with app.app_context():
        row = AuthorizedDirectory(path=str(d), alias="x", enabled=True)
        db.session.add(row)
        db.session.commit()
        yield d
        AuthorizedDirectory.query.filter_by(id=row.id).delete()
        db.session.commit()


class TestSearchFiles:
    def test_finds_matching_files_within_authorized_dir(self, app, authorized_dir):
        (authorized_dir / "a.txt").write_text("x")
        (authorized_dir / "b.txt").write_text("x")
        (authorized_dir / "c.md").write_text("x")
        with app.app_context():
            result = _run(mcp_tools._handle_search_files("search_files", {
                "path": str(authorized_dir), "pattern": "*.txt",
            }))
        assert "a.txt" in result
        assert "b.txt" in result
        assert "c.md" not in result

    def test_rejects_unauthorized_root_path(self, app, tmp_path):
        unauthorized = tmp_path / "not-authorized"
        unauthorized.mkdir()
        with app.app_context():
            result = _run(mcp_tools._handle_search_files("search_files", {
                "path": str(unauthorized), "pattern": "*",
            }))
        assert "not authorized" in result

    def test_pattern_cannot_traverse_outside_authorized_dir(self, app, authorized_dir, tmp_path):
        """Regression test: a pattern with ../ components used to return
        matches for files entirely outside the authorized directory —
        Path.glob() happily resolves and returns them, and only the root
        `path` argument was ever checked against _authorize_path, not each
        individual match. Confirmed exploitable before this fix (glob
        returned a PosixPath pointing at the sibling secret file)."""
        sibling = tmp_path / "sibling-not-authorized"
        sibling.mkdir()
        secret = sibling / "secret.txt"
        secret.write_text("TOP SECRET")

        with app.app_context():
            result = _run(mcp_tools._handle_search_files("search_files", {
                "path": str(authorized_dir),
                "pattern": f"../{sibling.name}/secret.txt",
            }))
        # "No files matching" (not "Found N matches") proves the traversal
        # match was filtered out — the pattern name itself legitimately
        # appears in that message, so check the absolute secret path
        # specifically, not just the substring "secret.txt".
        assert "No files matching" in result
        assert str(secret) not in result
        assert not result.startswith("Found")

    def test_no_matches_message(self, app, authorized_dir):
        with app.app_context():
            result = _run(mcp_tools._handle_search_files("search_files", {
                "path": str(authorized_dir), "pattern": "*.nonexistent",
            }))
        assert "No files matching" in result
