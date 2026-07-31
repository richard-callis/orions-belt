"""
Local document indexing / semantic search over files in authorized
directories.

Deliberately a SEPARATE pipeline from Memory/inject_context(). That's the
load-bearing design decision here: inject_context() splices recalled
memories into every future agent's system prompt globally — that's exactly
why Dream needed a human-review gate before anything it extracted could
reach it (an unreviewed, LLM-written memory is a real prompt-injection
surface into every future room). Indexed office documents must never work
that way. search_documents is called on demand, by name, like any other
tool — never auto-injected into a prompt. Otherwise every authorized
directory would become an unreviewed prompt-injection surface the moment
it's indexed.

Search backend: SQLite-stored embeddings + a NumPy cosine scan — the same
fallback path MemoryService already runs when LanceDB is unavailable,
which in practice is always on Windows (this app's primary platform; see
requirements.txt's `lancedb; platform_system != "Windows"` marker).
Deliberately NOT a second LanceDB integration for v1: bounded by
_MAX_INDEXED_CHUNKS, a linear NumPy scan stays well under 100ms, so there's
no case yet for the added complexity of a vector-store integration this
environment can't even exercise (lancedb isn't installed here either).
Revisit only if the cap needs to grow materially.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger("orions-belt.doc_index")

_SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}
_CHUNK_CHARS = 2000            # ~500 tokens, rough heuristic
_MAX_INDEXED_CHUNKS = 5000     # keeps the NumPy linear-scan search fast regardless of platform
_MAX_FILE_CHARS = 2_000_000    # bounds a single pathological file's extracted text


def _uuid() -> str:
    return str(uuid.uuid4())


def _extract_text(path: Path) -> str | None:
    """Best-effort text extraction. Returns None (never raises) for
    anything unreadable/unsupported/corrupt — one bad file must not abort
    indexing the rest of a directory."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".pdf":
            import pypdf
            reader = pypdf.PdfReader(str(path))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        if suffix == ".docx":
            import docx
            document = docx.Document(str(path))
            return "\n\n".join(p.text for p in document.paragraphs if p.text)
    except Exception as e:
        log.warning("doc_index: extraction failed for %s: %s", path, e)
        return None
    return None


def _chunk_text(text: str) -> list[str]:
    """Paragraph-aware chunking: pack whole paragraphs into ~_CHUNK_CHARS
    chunks; a single paragraph longer than that is hard-split so no chunk
    is ever unbounded."""
    if not text or not text.strip():
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > _CHUNK_CHARS:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), _CHUNK_CHARS):
                chunks.append(para[i:i + _CHUNK_CHARS])
            continue
        if current and len(current) + len(para) + 2 > _CHUNK_CHARS:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def index_directory(directory_id: str) -> dict:
    """(Re)index every supported file under one AuthorizedDirectory.

    A full reindex (deletes and replaces that directory's existing chunks),
    not incremental — simple, and fine at this module's corpus cap. Returns
    {"files_indexed", "chunks_created", "files_skipped", "capped"} or
    {"error": ...} if the directory isn't usable.
    """
    from app import db
    from app.models.connector import AuthorizedDirectory
    from app.models.doc_index import DocumentChunk
    from app.services.memory import get_memory_service
    import numpy as np

    directory = AuthorizedDirectory.query.get(directory_id)
    if not directory or not directory.enabled:
        return {"error": "directory not found or not enabled"}

    root = Path(directory.path)
    if not root.is_dir():
        return {"error": f"path does not exist: {directory.path}"}

    mem = get_memory_service()
    if mem.embed("doc_index availability probe") is None:
        # Bail before touching anything: every chunk written below would get
        # embedding=None and be permanently invisible to search_documents
        # (it only scans chunks with embedding.isnot(None)) — reporting
        # "success" here would be actively misleading, not just incomplete.
        return {"error": "embedding model is unavailable — indexing would produce an "
                          "unsearchable index (no chunk could be embedded); check the "
                          "embedding/memory service configuration (e.g. sentence-transformers "
                          "installation)"}

    # Compute the budget from OTHER directories' chunks BEFORE deleting this
    # directory's own existing chunks. If other directories already fill the
    # cap, budget is 0 — bail without deleting, so a reindex attempt that
    # can't succeed doesn't irreversibly destroy this directory's previously-
    # working index.
    this_dir_existing = DocumentChunk.query.filter_by(authorized_directory_id=directory_id).count()
    other_dirs_total = DocumentChunk.query.count() - this_dir_existing
    budget = max(0, _MAX_INDEXED_CHUNKS - other_dirs_total)
    if budget <= 0:
        return {"error": f"cannot reindex: the {_MAX_INDEXED_CHUNKS}-chunk index cap is "
                          f"already full from other directories — free up capacity (delete "
                          f"or disable another indexed directory) before reindexing this one"}

    DocumentChunk.query.filter_by(authorized_directory_id=directory_id).delete()
    db.session.commit()

    files_indexed = 0
    files_skipped = 0
    chunks_created = 0
    capped = False

    walker = root.rglob("*") if directory.recursive else root.glob("*")
    for path in sorted(walker):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            files_skipped += 1
            continue
        if capped:
            files_skipped += 1
            continue

        text = _extract_text(path)
        if not text:
            files_skipped += 1
            continue
        text = text[:_MAX_FILE_CHARS]

        chunks = _chunk_text(text)
        indexed_any = False
        for idx, chunk in enumerate(chunks):
            if chunks_created >= budget:
                capped = True
                break
            vec = mem.embed(chunk)
            embedding_bytes = np.array(vec, dtype="float32").tobytes() if vec else None
            db.session.add(DocumentChunk(
                id=_uuid(), authorized_directory_id=directory_id,
                file_path=str(path), chunk_index=idx, content=chunk,
                embedding=embedding_bytes,
            ))
            chunks_created += 1
            indexed_any = True
        if indexed_any:
            files_indexed += 1
        else:
            files_skipped += 1

    db.session.commit()
    if capped:
        log.warning("doc_index: hit the %d-chunk cap indexing directory %s — some files skipped",
                   _MAX_INDEXED_CHUNKS, directory_id)
    return {"files_indexed": files_indexed, "chunks_created": chunks_created,
            "files_skipped": files_skipped, "capped": capped}


def delete_directory_index(directory_id: str) -> int:
    """Remove all indexed chunks for a directory. Called when an
    AuthorizedDirectory is disabled or deleted, so revoking access to a
    directory actually removes its indexed content — not just blocks future
    indexing runs, which alone would leave already-indexed chunks
    searchable forever. Returns the number of chunks deleted."""
    from app import db
    from app.models.doc_index import DocumentChunk

    count = DocumentChunk.query.filter_by(authorized_directory_id=directory_id).count()
    if count:
        DocumentChunk.query.filter_by(authorized_directory_id=directory_id).delete()
        db.session.commit()
    return count


def search_documents(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over indexed document chunks.

    Re-validates authorization on every hit's source file at query time —
    the index is never trusted as an authorization record. Disabling or
    deleting an AuthorizedDirectory is honored immediately for every
    already-indexed chunk from that directory, not just future indexing.
    (AuthorizedDirectory.expires_at is NOT currently enforced anywhere in
    this app, including here — _authorize_path only checks `enabled`. If
    that changes app-wide, this re-check picks it up automatically since it
    goes through the same _authorize_path.)
    """
    from app.models.connector import AuthorizedDirectory
    from app.models.doc_index import DocumentChunk
    from app.services.memory import get_memory_service
    from app.services.mcp.tools import _authorize_path
    import numpy as np

    if not query or not query.strip():
        return []

    mem = get_memory_service()
    query_vec = mem.embed(query)
    if not query_vec:
        return []

    candidates = DocumentChunk.query.filter(DocumentChunk.embedding.isnot(None)).all()
    if not candidates:
        return []

    qv = np.asarray(query_vec, dtype="float32")
    qn = float(np.linalg.norm(qv)) or 1.0

    scored = []
    for chunk in candidates:
        try:
            vec = np.frombuffer(chunk.embedding, dtype="float32")
            if vec.size == 0 or vec.size != qv.size:
                continue
            denom = (float(np.linalg.norm(vec)) * qn) or 1.0
            scored.append((float(np.dot(vec, qv)) / denom, chunk))
        except Exception:
            continue
    scored.sort(key=lambda t: t[0], reverse=True)

    results = []
    # Over-fetch past top_k: some top-scored candidates may fail the
    # revocation re-check below and get dropped, and we still want top_k
    # genuinely-authorized results back, not fewer.
    for score, chunk in scored[: max(top_k * 3, top_k + 10)]:
        if len(results) >= top_k:
            break
        directory = AuthorizedDirectory.query.get(chunk.authorized_directory_id)
        if not directory or not directory.enabled:
            continue
        real_path = os.path.realpath(chunk.file_path)
        if not _authorize_path(real_path):
            continue
        if not os.path.isfile(real_path):
            continue  # file deleted/moved since indexing
        results.append({
            "file_path": chunk.file_path, "chunk_index": chunk.chunk_index,
            "content": chunk.content, "score": score,
        })
    return results
