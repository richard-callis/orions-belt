"""
Tests for the search_documents MCP tool (app/services/mcp/tools.py) and the
directory reindex/delete-on-disable routes (app/routes/mcp.py).
"""
import asyncio

import pytest

from app import db
from app.models.connector import AuthorizedDirectory
from app.models.doc_index import DocumentChunk
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSearchDocumentsTool:
    def test_requires_query(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_search_documents("search_documents", {}))
        assert "query is required" in result

    def test_no_results_message(self, app, monkeypatch):
        import app.services.doc_index as doc_index_mod
        monkeypatch.setattr(doc_index_mod, "search_documents", lambda q, top_k=5: [])
        with app.app_context():
            result = _run(mcp_tools._handle_search_documents("search_documents", {"query": "anything"}))
        assert "No matching documents" in result

    def test_wraps_results_in_untrusted_content_delimiters(self, app, monkeypatch):
        import app.services.doc_index as doc_index_mod
        monkeypatch.setattr(doc_index_mod, "search_documents", lambda q, top_k=5: [
            {"file_path": "/data/notes.txt", "chunk_index": 0, "content": "some content here", "score": 0.9},
        ])
        with app.app_context():
            result = _run(mcp_tools._handle_search_documents("search_documents", {"query": "notes"}))
        assert "<<<UNTRUSTED-DOCUMENT-CONTENT-" in result
        assert "<<<END-UNTRUSTED-DOCUMENT-CONTENT-" in result
        assert "/data/notes.txt" in result
        assert "some content here" in result

    def test_scans_results_for_pii_before_returning(self, app, monkeypatch):
        import app.services.doc_index as doc_index_mod
        import app.services.pii_guard as pii_guard_mod

        monkeypatch.setattr(doc_index_mod, "search_documents", lambda q, top_k=5: [
            {"file_path": "/data/notes.txt", "chunk_index": 0, "content": "contact alice@example.com", "score": 0.9},
        ])

        called = {"scan": False}
        class FakeGuard:
            def scan(self, text, session_id=None, message_id=None, direction="outbound"):
                called["scan"] = True
                return text.replace("alice@example.com", "[PII:EMAIL:xyz]"), True, ["EMAIL"]
        monkeypatch.setattr(pii_guard_mod, "get_pii_guard", lambda: FakeGuard())

        with app.app_context():
            result = _run(mcp_tools._handle_search_documents("search_documents", {"query": "contact"}))

        assert called["scan"] is True
        assert "alice@example.com" not in result
        assert "[PII:EMAIL:xyz]" in result

    def test_caps_top_k(self, app, monkeypatch):
        import app.services.doc_index as doc_index_mod
        captured = {}
        def fake_search(q, top_k=5):
            captured["top_k"] = top_k
            return []
        monkeypatch.setattr(doc_index_mod, "search_documents", fake_search)
        with app.app_context():
            _run(mcp_tools._handle_search_documents("search_documents", {"query": "q", "top_k": 9999}))
        assert captured["top_k"] == 20

    def test_search_error_returns_error_string_not_raise(self, app, monkeypatch):
        import app.services.doc_index as doc_index_mod
        def boom(q, top_k=5):
            raise RuntimeError("simulated failure")
        monkeypatch.setattr(doc_index_mod, "search_documents", boom)
        with app.app_context():
            result = _run(mcp_tools._handle_search_documents("search_documents", {"query": "q"}))
        assert "Error searching documents" in result


class TestDirectoryReindexRoute:
    def test_reindex_route_indexes_supported_files(self, app, client, tmp_path):
        (tmp_path / "a.txt").write_text("alpha content")
        with app.app_context():
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=True)
            db.session.add(d)
            db.session.commit()
            dir_id = d.id
        try:
            resp = client.post(f"/mcp/api/directories/{dir_id}/reindex")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["files_indexed"] == 1
            with app.app_context():
                assert DocumentChunk.query.filter_by(authorized_directory_id=dir_id).count() == 1
        finally:
            with app.app_context():
                DocumentChunk.query.filter_by(authorized_directory_id=dir_id).delete()
                AuthorizedDirectory.query.filter_by(id=dir_id).delete()
                db.session.commit()

    def test_reindex_unknown_directory_404s(self, app, client):
        resp = client.post("/mcp/api/directories/does-not-exist/reindex")
        assert resp.status_code == 404


class TestDeleteOnDisableHook:
    def test_disabling_a_directory_removes_its_index(self, app, client, tmp_path):
        with app.app_context():
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True)
            db.session.add(d)
            db.session.commit()
            dir_id = d.id
            db.session.add(DocumentChunk(authorized_directory_id=dir_id, file_path="a.txt",
                                         chunk_index=0, content="x"))
            db.session.commit()
        try:
            resp = client.patch(f"/mcp/api/directories/{dir_id}", json={"enabled": False})
            assert resp.status_code == 200
            with app.app_context():
                assert DocumentChunk.query.filter_by(authorized_directory_id=dir_id).count() == 0
        finally:
            with app.app_context():
                DocumentChunk.query.filter_by(authorized_directory_id=dir_id).delete()
                AuthorizedDirectory.query.filter_by(id=dir_id).delete()
                db.session.commit()

    def test_enabling_a_directory_does_not_touch_the_index(self, app, client, tmp_path):
        with app.app_context():
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=False)
            db.session.add(d)
            db.session.commit()
            dir_id = d.id
            db.session.add(DocumentChunk(authorized_directory_id=dir_id, file_path="a.txt",
                                         chunk_index=0, content="x"))
            db.session.commit()
        try:
            resp = client.patch(f"/mcp/api/directories/{dir_id}", json={"enabled": True})
            assert resp.status_code == 200
            with app.app_context():
                assert DocumentChunk.query.filter_by(authorized_directory_id=dir_id).count() == 1
        finally:
            with app.app_context():
                DocumentChunk.query.filter_by(authorized_directory_id=dir_id).delete()
                AuthorizedDirectory.query.filter_by(id=dir_id).delete()
                db.session.commit()

    def test_deleting_a_directory_removes_its_index(self, app, client, tmp_path):
        with app.app_context():
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True)
            db.session.add(d)
            db.session.commit()
            dir_id = d.id
            db.session.add(DocumentChunk(authorized_directory_id=dir_id, file_path="a.txt",
                                         chunk_index=0, content="x"))
            db.session.commit()
        resp = client.delete(f"/mcp/api/directories/{dir_id}")
        assert resp.status_code == 204
        with app.app_context():
            assert DocumentChunk.query.filter_by(authorized_directory_id=dir_id).count() == 0
