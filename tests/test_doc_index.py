"""
Tests for local document indexing/semantic search (app/services/doc_index.py).

sentence-transformers isn't installed in this environment (same as the
existing Memory feature — see the "Memory Service: embedding model
unavailable" log line at startup), so search-ranking tests hand-craft
DocumentChunk rows with known embedding bytes and monkeypatch
MemoryService.embed to return a matching query vector — the same approach
tests/test_memory_fallback.py already uses for Memory's own NumPy fallback
path. Extraction (_extract_text) and chunking (_chunk_text) are tested
against real files with no mocking — pypdf and python-docx are both real
dependencies.
"""
import numpy as np
import pytest

from app import db
from app.models.connector import AuthorizedDirectory
from app.models.doc_index import DocumentChunk
import app.services.doc_index as doc_index_mod
import app.services.memory as memory_mod


def _cleanup_chunks(directory_id):
    DocumentChunk.query.filter_by(authorized_directory_id=directory_id).delete()
    db.session.commit()


def _cleanup_dir(dir_id):
    AuthorizedDirectory.query.filter_by(id=dir_id).delete()
    db.session.commit()


class TestExtractText:
    def test_reads_txt_file(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("hello world\nsecond line")
        assert doc_index_mod._extract_text(f) == "hello world\nsecond line"

    def test_reads_md_file(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Title\n\nBody text")
        assert "Body text" in doc_index_mod._extract_text(f)

    def test_reads_real_pdf(self, tmp_path):
        from reportlab.platypus import SimpleDocTemplate, Paragraph
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.pagesizes import LETTER

        f = tmp_path / "doc.pdf"
        styles = getSampleStyleSheet()
        doc = SimpleDocTemplate(str(f), pagesize=LETTER)
        doc.build([Paragraph("Extracted PDF content marker XYZ123", styles["Normal"])])

        text = doc_index_mod._extract_text(f)
        assert text is not None
        assert "XYZ123" in text

    def test_reads_real_docx(self, tmp_path):
        import docx

        f = tmp_path / "doc.docx"
        document = docx.Document()
        document.add_paragraph("DOCX content marker ABC789")
        document.save(str(f))

        text = doc_index_mod._extract_text(f)
        assert "ABC789" in text

    def test_unsupported_extension_returns_none(self, tmp_path):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n")
        assert doc_index_mod._extract_text(f) is None

    def test_corrupt_pdf_returns_none_not_raises(self, tmp_path):
        f = tmp_path / "broken.pdf"
        f.write_bytes(b"not a real pdf")
        assert doc_index_mod._extract_text(f) is None


class TestChunkText:
    def test_empty_text_returns_no_chunks(self):
        assert doc_index_mod._chunk_text("") == []
        assert doc_index_mod._chunk_text("   ") == []

    def test_short_text_is_one_chunk(self):
        chunks = doc_index_mod._chunk_text("short paragraph")
        assert chunks == ["short paragraph"]

    def test_packs_multiple_paragraphs_into_one_chunk(self):
        text = "para one" + "\n\n" + "para two"
        chunks = doc_index_mod._chunk_text(text)
        assert len(chunks) == 1
        assert "para one" in chunks[0] and "para two" in chunks[0]

    def test_splits_when_exceeding_chunk_size(self):
        big_para = "x" * (doc_index_mod._CHUNK_CHARS - 10)
        text = big_para + "\n\n" + "y" * 500
        chunks = doc_index_mod._chunk_text(text)
        assert len(chunks) == 2

    def test_hard_splits_a_single_oversized_paragraph(self):
        huge = "z" * (doc_index_mod._CHUNK_CHARS * 2 + 100)
        chunks = doc_index_mod._chunk_text(huge)
        assert len(chunks) >= 2
        assert all(len(c) <= doc_index_mod._CHUNK_CHARS for c in chunks)
        assert "".join(chunks) == huge


class TestIndexDirectory:
    def test_indexes_supported_files_and_skips_others(self, app, tmp_path, monkeypatch):
        monkeypatch.setattr(memory_mod, "get_memory_service",
                            lambda: type("M", (), {"embed": staticmethod(lambda t: [1.0, 0.0])})())
        with app.app_context():
            (tmp_path / "a.txt").write_text("alpha content here")
            (tmp_path / "b.md").write_text("beta content here")
            (tmp_path / "ignore.png").write_bytes(b"\x89PNG")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=True)
            db.session.add(d)
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert result["files_indexed"] == 2
                assert result["files_skipped"] == 1
                chunks = DocumentChunk.query.filter_by(authorized_directory_id=d.id).all()
                assert len(chunks) == 2
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_non_recursive_skips_subdirectories(self, app, tmp_path, monkeypatch):
        monkeypatch.setattr(memory_mod, "get_memory_service",
                            lambda: type("M", (), {"embed": staticmethod(lambda t: [1.0, 0.0])})())
        with app.app_context():
            (tmp_path / "top.txt").write_text("top level")
            sub = tmp_path / "sub"
            sub.mkdir()
            (sub / "nested.txt").write_text("nested content")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=False)
            db.session.add(d)
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert result["files_indexed"] == 1
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_recursive_includes_subdirectories(self, app, tmp_path, monkeypatch):
        monkeypatch.setattr(memory_mod, "get_memory_service",
                            lambda: type("M", (), {"embed": staticmethod(lambda t: [1.0, 0.0])})())
        with app.app_context():
            (tmp_path / "top.txt").write_text("top level")
            sub = tmp_path / "sub"
            sub.mkdir()
            (sub / "nested.txt").write_text("nested content")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=True)
            db.session.add(d)
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert result["files_indexed"] == 2
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_reindex_replaces_old_chunks(self, app, tmp_path, monkeypatch):
        monkeypatch.setattr(memory_mod, "get_memory_service",
                            lambda: type("M", (), {"embed": staticmethod(lambda t: [1.0, 0.0])})())
        with app.app_context():
            f = tmp_path / "a.txt"
            f.write_text("version one")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=True)
            db.session.add(d)
            db.session.commit()
            try:
                doc_index_mod.index_directory(d.id)
                first_count = DocumentChunk.query.filter_by(authorized_directory_id=d.id).count()

                f.write_text("version two, quite different and longer content here")
                doc_index_mod.index_directory(d.id)
                chunks = DocumentChunk.query.filter_by(authorized_directory_id=d.id).all()
                assert all("version one" not in c.content for c in chunks)
                assert any("version two" in c.content for c in chunks)
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_disabled_directory_rejected(self, app, tmp_path):
        with app.app_context():
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=False, recursive=True)
            db.session.add(d)
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert "error" in result
            finally:
                _cleanup_dir(d.id)

    def test_missing_path_rejected(self, app, tmp_path):
        with app.app_context():
            d = AuthorizedDirectory(path=str(tmp_path / "does-not-exist"), alias="x", enabled=True)
            db.session.add(d)
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert "error" in result
            finally:
                _cleanup_dir(d.id)

    def test_corpus_cap_is_enforced(self, app, tmp_path, monkeypatch):
        monkeypatch.setattr(doc_index_mod, "_MAX_INDEXED_CHUNKS", 3)
        monkeypatch.setattr(memory_mod, "get_memory_service",
                            lambda: type("M", (), {"embed": staticmethod(lambda t: [1.0, 0.0])})())
        with app.app_context():
            for i in range(5):
                # Each file's content is one paragraph -> exactly one chunk.
                (tmp_path / f"f{i}.txt").write_text(f"content of file {i}")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=True)
            db.session.add(d)
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert result["capped"] is True
                assert result["chunks_created"] == 3
                total = DocumentChunk.query.filter_by(authorized_directory_id=d.id).count()
                assert total == 3
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_partially_indexed_file_reported_separately_from_fully_indexed(self, app, tmp_path, monkeypatch):
        """Regression test: previously, a file whose OWN chunks were cut off
        mid-way by the corpus cap was still counted under files_indexed,
        overstating how much of it is actually searchable — some of its
        content never got a chunk written at all."""
        monkeypatch.setattr(doc_index_mod, "_MAX_INDEXED_CHUNKS", 1)
        monkeypatch.setattr(memory_mod, "get_memory_service",
                            lambda: type("M", (), {"embed": staticmethod(lambda t: [1.0, 0.0])})())
        with app.app_context():
            # Two paragraphs -> _chunk_text produces two separate chunks for
            # this one file (see TestChunkText.test_splits_when_exceeding_chunk_size).
            big = "x" * (doc_index_mod._CHUNK_CHARS - 10)
            (tmp_path / "a.txt").write_text(big + "\n\n" + "y" * 500)
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=True)
            db.session.add(d)
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert result["files_indexed"] == 0
                assert result["files_partially_indexed"] == 1
                assert result["chunks_created"] == 1
                assert result["capped"] is True
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_returns_error_and_indexes_nothing_when_embedding_model_unavailable(self, app, tmp_path, monkeypatch):
        """Regression test: previously, when mem.embed() always returned
        None (e.g. sentence-transformers not installed), index_directory
        still reported success while writing chunks with embedding=None —
        a completely unsearchable index that LOOKED like it worked. It must
        now refuse instead, and touch nothing on disk (no existing index
        destroyed by a doomed reindex attempt)."""
        monkeypatch.setattr(memory_mod, "get_memory_service",
                            lambda: type("M", (), {"embed": staticmethod(lambda t: None)})())
        with app.app_context():
            (tmp_path / "a.txt").write_text("alpha content here")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=True)
            db.session.add(d)
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert "error" in result
                assert DocumentChunk.query.filter_by(authorized_directory_id=d.id).count() == 0
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_does_not_delete_existing_index_when_cap_is_full_from_other_directories(self, app, tmp_path, monkeypatch):
        """Regression test: previously the delete ran BEFORE the budget was
        computed, so a reindex attempt that couldn't succeed (cap already
        full from OTHER directories) would still wipe this directory's own
        working index first, leaving it empty with no way to rebuild."""
        monkeypatch.setattr(doc_index_mod, "_MAX_INDEXED_CHUNKS", 1)
        monkeypatch.setattr(memory_mod, "get_memory_service",
                            lambda: type("M", (), {"embed": staticmethod(lambda t: [1.0, 0.0])})())
        with app.app_context():
            other_path = tmp_path.parent / (tmp_path.name + "-other-full")
            other_path.mkdir()
            other_dir = AuthorizedDirectory(path=str(other_path), alias="other", enabled=True)
            db.session.add(other_dir)
            db.session.commit()
            db.session.add(DocumentChunk(authorized_directory_id=other_dir.id, file_path="x.txt",
                                         chunk_index=0, content="fills the cap"))
            db.session.commit()

            (tmp_path / "a.txt").write_text("existing content")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True, recursive=True)
            db.session.add(d)
            db.session.commit()
            db.session.add(DocumentChunk(authorized_directory_id=d.id, file_path=str(tmp_path / "a.txt"),
                                         chunk_index=0, content="existing content"))
            db.session.commit()
            try:
                result = doc_index_mod.index_directory(d.id)
                assert "error" in result
                # The pre-existing chunk must survive — nothing was deleted.
                assert DocumentChunk.query.filter_by(authorized_directory_id=d.id).count() == 1
            finally:
                _cleanup_chunks(d.id)
                _cleanup_chunks(other_dir.id)
                _cleanup_dir(d.id)
                _cleanup_dir(other_dir.id)


class TestDeleteDirectoryIndex:
    def test_removes_only_that_directorys_chunks(self, app, tmp_path):
        with app.app_context():
            d1 = AuthorizedDirectory(path=str(tmp_path), alias="d1", enabled=True)
            d2_path = tmp_path.parent / (tmp_path.name + "-other")
            d2_path.mkdir()
            d2 = AuthorizedDirectory(path=str(d2_path), alias="d2", enabled=True)
            db.session.add_all([d1, d2])
            db.session.commit()
            db.session.add(DocumentChunk(authorized_directory_id=d1.id, file_path="a.txt",
                                         chunk_index=0, content="x"))
            db.session.add(DocumentChunk(authorized_directory_id=d2.id, file_path="b.txt",
                                         chunk_index=0, content="y"))
            db.session.commit()
            try:
                deleted = doc_index_mod.delete_directory_index(d1.id)
                assert deleted == 1
                assert DocumentChunk.query.filter_by(authorized_directory_id=d1.id).count() == 0
                assert DocumentChunk.query.filter_by(authorized_directory_id=d2.id).count() == 1
            finally:
                _cleanup_chunks(d1.id)
                _cleanup_chunks(d2.id)
                _cleanup_dir(d1.id)
                _cleanup_dir(d2.id)


class _FakeMem:
    """Deterministic stand-in for MemoryService.embed — sentence-transformers
    isn't installed in this environment, so search-ranking tests use known
    vectors instead of the real model, same approach
    test_memory_fallback.py uses for Memory's own NumPy path."""
    def __init__(self, query_vec):
        self._query_vec = query_vec

    def embed(self, text):
        return self._query_vec


class TestSearchDocuments:
    def _chunk(self, directory_id, file_path, vec, content="content", chunk_index=0):
        return DocumentChunk(
            authorized_directory_id=directory_id, file_path=file_path,
            chunk_index=chunk_index, content=content,
            embedding=np.asarray(vec, dtype="float32").tobytes(),
        )

    def test_orders_by_similarity_and_respects_top_k(self, app, tmp_path, monkeypatch):
        with app.app_context():
            f1 = tmp_path / "close.txt"; f1.write_text("x")
            f2 = tmp_path / "mid.txt"; f2.write_text("x")
            f3 = tmp_path / "far.txt"; f3.write_text("x")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True)
            db.session.add(d)
            db.session.commit()
            db.session.add_all([
                self._chunk(d.id, str(f1), [1.0, 0.0, 0.0]),
                self._chunk(d.id, str(f2), [0.7, 0.7, 0.0]),
                self._chunk(d.id, str(f3), [0.0, 1.0, 0.0]),
            ])
            db.session.commit()
            monkeypatch.setattr(memory_mod, "get_memory_service", lambda: _FakeMem([1.0, 0.0, 0.0]))
            try:
                results = doc_index_mod.search_documents("query", top_k=2)
                assert len(results) == 2
                assert results[0]["file_path"] == str(f1)
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_excludes_chunks_from_disabled_directory(self, app, tmp_path, monkeypatch):
        with app.app_context():
            f = tmp_path / "a.txt"; f.write_text("x")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=False)  # already disabled
            db.session.add(d)
            db.session.commit()
            db.session.add(self._chunk(d.id, str(f), [1.0, 0.0]))
            db.session.commit()
            monkeypatch.setattr(memory_mod, "get_memory_service", lambda: _FakeMem([1.0, 0.0]))
            try:
                results = doc_index_mod.search_documents("query", top_k=5)
                assert results == []
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_revocation_takes_effect_immediately_for_already_indexed_chunks(self, app, tmp_path, monkeypatch):
        # The index must never be trusted as an authorization record —
        # disabling a directory AFTER indexing must exclude its chunks from
        # search results right away, not just block future indexing.
        with app.app_context():
            f = tmp_path / "a.txt"; f.write_text("x")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True)
            db.session.add(d)
            db.session.commit()
            db.session.add(self._chunk(d.id, str(f), [1.0, 0.0]))
            db.session.commit()
            monkeypatch.setattr(memory_mod, "get_memory_service", lambda: _FakeMem([1.0, 0.0]))
            try:
                assert len(doc_index_mod.search_documents("query", top_k=5)) == 1
                d.enabled = False
                db.session.commit()
                assert doc_index_mod.search_documents("query", top_k=5) == []
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_excludes_chunk_whose_source_file_was_deleted(self, app, tmp_path, monkeypatch):
        with app.app_context():
            f = tmp_path / "gone.txt"
            f.write_text("x")
            d = AuthorizedDirectory(path=str(tmp_path), alias="x", enabled=True)
            db.session.add(d)
            db.session.commit()
            db.session.add(self._chunk(d.id, str(f), [1.0, 0.0]))
            db.session.commit()
            f.unlink()  # file removed from disk after indexing
            monkeypatch.setattr(memory_mod, "get_memory_service", lambda: _FakeMem([1.0, 0.0]))
            try:
                assert doc_index_mod.search_documents("query", top_k=5) == []
            finally:
                _cleanup_chunks(d.id)
                _cleanup_dir(d.id)

    def test_empty_query_returns_empty(self, app):
        with app.app_context():
            assert doc_index_mod.search_documents("", top_k=5) == []

    def test_no_index_returns_empty(self, app, monkeypatch):
        monkeypatch.setattr(memory_mod, "get_memory_service", lambda: _FakeMem([1.0, 0.0]))
        with app.app_context():
            assert doc_index_mod.search_documents("nothing indexed", top_k=5) == []
