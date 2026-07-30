"""
Tests for the authorized-directories hint added to path-not-authorized
errors, and to create_directory/list_directory. Fixes a live usability bug:
an agent guessing a bare relative path (e.g. "testing2") got refused with no
way to self-correct, since nothing told it what absolute paths were valid.
"""
import asyncio

from app import db
from app.models.connector import AuthorizedDirectory
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestAuthorizedDirsHint:
    def test_hint_lists_configured_directories(self, app):
        with app.app_context():
            db.session.add(AuthorizedDirectory(path="/data/projects", alias="Projects", enabled=True))
            db.session.commit()
            try:
                hint = mcp_tools._authorized_dirs_hint()
                assert "Projects=/data/projects" in hint
            finally:
                AuthorizedDirectory.query.filter_by(path="/data/projects").delete()
                db.session.commit()

    def test_hint_reports_none_configured(self, app):
        with app.app_context():
            hint = mcp_tools._authorized_dirs_hint()
            assert "no authorized directories" in hint

    def test_hint_excludes_disabled_directories(self, app):
        with app.app_context():
            db.session.add(AuthorizedDirectory(path="/data/off", alias="Off", enabled=False))
            db.session.commit()
            try:
                hint = mcp_tools._authorized_dirs_hint()
                assert "Off" not in hint
            finally:
                AuthorizedDirectory.query.filter_by(path="/data/off").delete()
                db.session.commit()


class TestCreateDirectoryErrorIncludesHint(object):
    def test_relative_path_error_includes_authorized_dirs(self, app):
        with app.app_context():
            db.session.add(AuthorizedDirectory(path="/data/projects", alias="Projects", enabled=True))
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_create_directory("create_directory", {"path": "testing2"}))
                assert "not authorized" in result
                assert "Projects=/data/projects" in result
            finally:
                AuthorizedDirectory.query.filter_by(path="/data/projects").delete()
                db.session.commit()
