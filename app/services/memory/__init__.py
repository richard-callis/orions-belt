"""
Orion's Belt — Memory Service
Persistent cross-session memory with semantic similarity recall.

Backend: LanceDB (replaces SQLite LargeBinary + numpy cosine loop).
Public API is unchanged — callers see no difference.

Usage:
    mem = get_memory_service()
    mem.store("User prefers Python", "The user is a Python developer", source="user")
    memories = mem.recall("what language should I use?", top_k=5)
    context = mem.inject_context("what language?")
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Optional

log = logging.getLogger("orions-belt.memory")

_instance: Optional["MemoryService"] = None
_lock = threading.Lock()


def get_memory_service() -> "MemoryService":
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = MemoryService()
    return _instance


class MemoryService:
    """Embedding-based persistent memory backed by LanceDB."""

    def __init__(self):
        self._model = None
        self._model_ready = False
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure_initialized(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._init_model()
            self._initialized = True

    def _init_model(self):
        try:
            from sentence_transformers import SentenceTransformer
            from config import Config
            model_name = getattr(Config, "MEMORY_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
            self._model = SentenceTransformer(model_name)
            self._model_ready = True
            log.info("Memory Service: embedding model loaded (%s)", model_name)

            # Run SQLite → LanceDB migration if the LanceDB table is empty
            self._maybe_migrate()
        except Exception as e:
            log.warning("Memory Service: embedding model unavailable (%s) — recall disabled", e)

    def _maybe_migrate(self):
        """Migrate SQLite embeddings into LanceDB on first startup."""
        try:
            from app.services.memory.lance_store import get_lance_store, migrate_from_sqlite
            from app.models.memory import Memory
            store = get_lance_store()
            if store.count() == 0:
                n = migrate_from_sqlite(Memory)
                if n:
                    log.info("Memory Service: migrated %d records from SQLite to LanceDB", n)
        except Exception as e:
            log.warning("Memory Service: migration skipped — %s", e)

    def _embed(self, text: str) -> list[float] | None:
        if not self._model_ready or not self._model:
            return None
        try:
            import numpy as np
            vec = self._model.encode(text, normalize_embeddings=True)
            return vec.astype("float32").tolist()
        except Exception as e:
            log.debug("Memory Service: embed error: %s", e)
            return None

    def store(
        self,
        title: str,
        content: str,
        memory_type: str = "persistent",
        scope: dict | None = None,
        source: str = "user",
        pinned: bool = False,
    ):
        """Store a new memory with its embedding in both SQLite and LanceDB."""
        self._ensure_initialized()

        from app import db
        from app.models.memory import Memory
        import numpy as np

        scope = scope or {}
        mem_id = str(uuid.uuid4())

        # Compute embedding
        vec = self._embed(content)
        embedding_bytes = np.array(vec, dtype="float32").tobytes() if vec else None

        # SQLite record (source of truth for UI queries)
        mem = Memory(
            id=mem_id,
            memory_type=memory_type,
            title=title,
            content=content,
            embedding=embedding_bytes,
            source=source,
            pinned=pinned,
            scope_project_id=scope.get("project_id"),
            scope_epic_id=scope.get("epic_id"),
            scope_task_id=scope.get("task_id"),
            scope_connector_id=scope.get("connector_id"),
        )
        db.session.add(mem)
        db.session.commit()

        # LanceDB record (for vector search)
        if vec:
            try:
                from app.services.memory.lance_store import get_lance_store
                from datetime import datetime, timezone
                get_lance_store().add({
                    "id":                 mem_id,
                    "title":              title,
                    "content":            content,
                    "memory_type":        memory_type,
                    "source":             source,
                    "pinned":             pinned,
                    "scope_project_id":   scope.get("project_id") or "",
                    "scope_epic_id":      scope.get("epic_id") or "",
                    "scope_task_id":      scope.get("task_id") or "",
                    "scope_connector_id": scope.get("connector_id") or "",
                    "created_at":         datetime.now(timezone.utc).isoformat(),
                }, vec)
            except Exception as e:
                log.warning("Memory Service: LanceDB write failed (SQLite record saved): %s", e)

        log.debug("Memory Service: stored '%s'", title)
        return mem

    def recall(
        self,
        query: str,
        top_k: int | None = None,
        scope_filter: dict | None = None,
    ) -> list:
        """Return the top-k most semantically similar memories via LanceDB ANN search.

        Falls back to recent SQLite memories if LanceDB or embeddings unavailable.
        """
        self._ensure_initialized()

        from app.models.memory import Memory
        from config import Config

        k = top_k or getattr(Config, "MEMORY_TOP_K", 5)

        # Always include pinned memories (from SQLite — authoritative)
        pinned = Memory.query.filter_by(pinned=True).all()

        if not self._model_ready:
            recent = Memory.query.order_by(Memory.created_at.desc()).limit(k).all()
            seen = {m.id for m in pinned}
            result = list(pinned)
            for m in recent:
                if m.id not in seen:
                    result.append(m)
            return result[:k]

        query_vec = self._embed(query)
        if not query_vec:
            return pinned[:k]

        try:
            from app.services.memory.lance_store import get_lance_store
            rows = get_lance_store().search(query_vec, top_k=k, scope_filter=scope_filter)
        except Exception as e:
            log.warning("Memory Service: LanceDB search failed, falling back: %s", e)
            rows = []

        # Map LanceDB rows back to SQLite Memory objects for API compatibility
        if rows:
            ids = [r["id"] for r in rows]
            similar_mems = Memory.query.filter(Memory.id.in_(ids)).all()
            # Preserve LanceDB score order
            order_map = {r["id"]: i for i, r in enumerate(rows)}
            similar_mems.sort(key=lambda m: order_map.get(m.id, 999))
        else:
            similar_mems = []

        seen_ids = {m.id for m in pinned}
        result = list(pinned)
        for m in similar_mems:
            if m.id not in seen_ids:
                result.append(m)
                seen_ids.add(m.id)

        return result[:k]

    def inject_context(self, query: str, session_id: str | None = None) -> str:
        """Build a system context string from recalled memories."""
        memories = self.recall(query)
        if not memories:
            return ""
        lines = ["--- Relevant Context from Memory ---"]
        for m in memories:
            lines.append(f"[{m.memory_type.upper()}] {m.title}: {m.content}")
        lines.append("--- End of Memory Context ---")
        return "\n".join(lines)

    def delete(self, memory_id: str) -> bool:
        """Delete a memory from both SQLite and LanceDB."""
        from app import db
        from app.models.memory import Memory

        m = Memory.query.get(memory_id)
        if not m:
            return False
        db.session.delete(m)
        db.session.commit()

        try:
            from app.services.memory.lance_store import get_lance_store
            get_lance_store().delete(memory_id)
        except Exception as e:
            log.warning("Memory Service: LanceDB delete failed id=%s: %s", memory_id, e)

        return True

    @property
    def status(self) -> str:
        if not self._initialized:
            return "not_loaded"
        return "ready" if self._model_ready else "degraded"
