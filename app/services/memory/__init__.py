"""
Orion's Belt — Memory Service
Persistent cross-session memory with semantic similarity recall.

Stores facts, project context, and entity knowledge as embeddings.
On recall, computes cosine similarity to find the most relevant memories
for the current query and injects them as LLM system context.

Usage:
    mem = get_memory_service()
    mem.store("User prefers Python", "The user is a Python developer", source="user")
    memories = mem.recall("what language should I use?", top_k=5)
    context = mem.inject_context("what language?", session_id="abc")
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Optional

import numpy as np

log = logging.getLogger("orions-belt.memory")

_instance: Optional["MemoryService"] = None
_lock = threading.Lock()


def get_memory_service() -> "MemoryService":
    """Return the singleton MemoryService instance."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = MemoryService()
    return _instance


class MemoryService:
    """Embedding-based persistent memory with cosine similarity recall."""

    def __init__(self):
        self._model = None
        self._model_ready = False
        self._init_lock = threading.Lock()
        self._initialized = False

    # ── Lazy initialization ───────────────────────────────────────────────────

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
            log.info(f"Memory Service: embedding model loaded ({model_name})")
        except Exception as e:
            log.warning(f"Memory Service: embedding model unavailable ({e}) — similarity recall disabled")

    # ── Core API ──────────────────────────────────────────────────────────────

    def _embed(self, text: str) -> bytes | None:
        """Compute an embedding for text and return as raw bytes."""
        if not self._model_ready or not self._model:
            return None
        try:
            vec = self._model.encode(text, normalize_embeddings=True)
            return vec.astype(np.float32).tobytes()
        except Exception as e:
            log.debug(f"Memory Service: embed error: {e}")
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
        """Store a new memory with its embedding.

        Args:
            title: Short label for this memory (shown in UI)
            content: Full memory text
            memory_type: "persistent" | "project" | "entity"
            scope: Optional dict with keys: project_id, epic_id, task_id, connector_id
            source: "user" | "agent" | "system"
            pinned: If True, always injected regardless of similarity score

        Returns:
            The created Memory ORM instance
        """
        self._ensure_initialized()

        from app import db
        from app.models.memory import Memory

        embedding_bytes = self._embed(content)
        scope = scope or {}

        mem = Memory(
            id=str(uuid.uuid4()),
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
        log.debug(f"Memory Service: stored memory '{title}'")
        return mem

    def recall(
        self,
        query: str,
        top_k: int | None = None,
        scope_filter: dict | None = None,
    ) -> list:
        """Return the top-k most semantically similar memories.

        Args:
            query: The text to compare memories against
            top_k: Number of results (defaults to Config.MEMORY_TOP_K)
            scope_filter: Dict to filter by scope (project_id, task_id, etc.)

        Returns:
            List of Memory ORM objects ordered by similarity (most similar first)
        """
        self._ensure_initialized()

        from app.models.memory import Memory

        try:
            from config import Config
            default_k = getattr(Config, "MEMORY_TOP_K", 5)
        except Exception:
            default_k = 5

        k = top_k or default_k

        # Always include pinned memories
        pinned_q = Memory.query.filter_by(pinned=True)
        pinned = pinned_q.all()

        if not self._model_ready:
            # No embeddings — fall back to returning recent memories
            recent = Memory.query.order_by(Memory.created_at.desc()).limit(k).all()
            all_mems = pinned + [m for m in recent if not m.pinned]
            return all_mems[:k]

        # Embed the query
        query_bytes = self._embed(query)
        if not query_bytes:
            return pinned[:k]

        query_vec = np.frombuffer(query_bytes, dtype=np.float32)

        # Load all non-pinned memories
        q_all = Memory.query.filter_by(pinned=False)
        if scope_filter:
            for field, val in scope_filter.items():
                col = f"scope_{field}"
                if hasattr(Memory, col):
                    q_all = q_all.filter(getattr(Memory, col) == val)
        all_mems = q_all.all()

        # Compute cosine similarity
        scored = []
        for m in all_mems:
            if not m.embedding:
                continue
            try:
                mem_vec = np.frombuffer(m.embedding, dtype=np.float32)
                if mem_vec.shape == query_vec.shape:
                    similarity = float(np.dot(query_vec, mem_vec))  # vecs are normalized
                    scored.append((similarity, m))
            except Exception:
                continue

        scored.sort(key=lambda x: x[0], reverse=True)
        top_similar = [m for _, m in scored[:k]]

        # Merge pinned + similar (deduplicated by ID)
        seen_ids = {m.id for m in pinned}
        result = list(pinned)
        for m in top_similar:
            if m.id not in seen_ids:
                result.append(m)
                seen_ids.add(m.id)

        return result[:k]

    def inject_context(self, query: str, session_id: str | None = None) -> str:
        """Build a system context string from recalled memories.

        Returns empty string if no relevant memories exist.
        Prepend this to the system prompt before each LLM call.
        """
        memories = self.recall(query)
        if not memories:
            return ""

        lines = ["--- Relevant Context from Memory ---"]
        for m in memories:
            lines.append(f"[{m.memory_type.upper()}] {m.title}: {m.content}")
        lines.append("--- End of Memory Context ---")
        return "\n".join(lines)

    def delete(self, memory_id: str) -> bool:
        """Delete a memory by ID. Returns True if deleted, False if not found."""
        from app import db
        from app.models.memory import Memory
        m = Memory.query.get(memory_id)
        if not m:
            return False
        db.session.delete(m)
        db.session.commit()
        return True

    @property
    def status(self) -> str:
        """Return current operational status."""
        if not self._initialized:
            return "not_loaded"
        return "ready" if self._model_ready else "degraded"
