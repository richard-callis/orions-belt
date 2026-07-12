"""
LanceDB vector store for Orion's Belt memory.

Stores memory embeddings in a LanceDB table alongside metadata.
Replaces the SQLite LargeBinary + numpy cosine loop.

The table lives at Config.LANCE_DB_PATH (default: <project_root>/memory.lance/).
On first access it is created from scratch. If existing SQLite memories exist,
call migrate_from_sqlite() once to backfill.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("orions-belt.memory.lance")

_EMBEDDING_DIM = 384   # all-MiniLM-L6-v2 output dimension
_TABLE_NAME = "memories"

_instance: "LanceStore | None" = None
_lock = threading.Lock()


def get_lance_store() -> "LanceStore":
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = LanceStore()
    return _instance


class LanceStore:
    """Thin wrapper around a LanceDB table for memory storage and ANN search."""

    def __init__(self):
        self._db = None
        self._table = None
        self._ready = False
        self._init_lock = threading.Lock()

    def _ensure_ready(self):
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            self._init()
            self._ready = True

    def _init(self):
        try:
            import lancedb
            import pyarrow as pa
            from config import Config

            db_path = str(getattr(Config, "LANCE_DB_PATH",
                          Path(Config.DATABASE_PATH).parent / "memory.lance"))
            self._db = lancedb.connect(db_path)

            schema = pa.schema([
                pa.field("id",                 pa.string()),
                pa.field("title",              pa.string()),
                pa.field("content",            pa.string()),
                pa.field("memory_type",        pa.string()),
                pa.field("source",             pa.string()),
                pa.field("pinned",             pa.bool_()),
                pa.field("scope_project_id",   pa.string()),
                pa.field("scope_epic_id",      pa.string()),
                pa.field("scope_task_id",      pa.string()),
                pa.field("scope_connector_id", pa.string()),
                pa.field("created_at",         pa.string()),
                pa.field("vector", pa.list_(pa.float32(), _EMBEDDING_DIM)),
            ])

            existing = self._db.table_names()
            if _TABLE_NAME not in existing:
                self._table = self._db.create_table(_TABLE_NAME, schema=schema)
                log.info("LanceDB table created: %s", db_path)
            else:
                self._table = self._db.open_table(_TABLE_NAME)
                log.info("LanceDB table opened: %s (%d rows)", db_path, self._table.count_rows())

        except Exception as e:
            log.error("LanceDB init failed: %s", e)
            self._ready = False
            raise

    def add(self, record: dict, vector: list[float]):
        """Insert one memory record with its embedding vector."""
        self._ensure_ready()
        import pyarrow as pa

        # Pad or truncate vector to expected dimension
        v = list(vector)
        if len(v) < _EMBEDDING_DIM:
            v += [0.0] * (_EMBEDDING_DIM - len(v))
        v = v[:_EMBEDDING_DIM]

        row = {
            "id":                 record.get("id", ""),
            "title":              record.get("title", ""),
            "content":            record.get("content", ""),
            "memory_type":        record.get("memory_type", "persistent"),
            "source":             record.get("source", "user"),
            "pinned":             bool(record.get("pinned", False)),
            "scope_project_id":   record.get("scope_project_id") or "",
            "scope_epic_id":      record.get("scope_epic_id") or "",
            "scope_task_id":      record.get("scope_task_id") or "",
            "scope_connector_id": record.get("scope_connector_id") or "",
            "created_at":         record.get("created_at", ""),
            "vector":             v,
        }
        self._table.add([row])

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        scope_filter: dict | None = None,
    ) -> list[dict]:
        """ANN search — returns top_k most similar rows as dicts."""
        self._ensure_ready()

        query = self._table.search(query_vector).limit(top_k)

        # Apply scope filter as a post-filter (LanceDB supports WHERE clauses)
        if scope_filter:
            conditions = []
            field_map = {
                "project_id":   "scope_project_id",
                "epic_id":      "scope_epic_id",
                "task_id":      "scope_task_id",
                "connector_id": "scope_connector_id",
            }
            for k, v in scope_filter.items():
                col = field_map.get(k, k)
                # Validate value is a safe identifier (UUID or alphanumeric) before
                # interpolating into the WHERE clause — LanceDB has no bind parameters.
                if not re.match(r'^[a-zA-Z0-9_-]+$', str(v)):
                    log.warning("lance_store: skipping unsafe scope_filter value for %s", col)
                    continue
                conditions.append(f"{col} = '{v}'")
            if conditions:
                query = query.where(" AND ".join(conditions))

        try:
            results = query.to_list()
        except Exception as e:
            log.warning("LanceDB search failed: %s", e)
            return []

        return results

    def get_pinned(self) -> list[dict]:
        """Return all pinned memories (always included in context)."""
        self._ensure_ready()
        try:
            results = self._table.search().where("pinned = true").limit(50).to_list()
            return results
        except Exception as e:
            log.warning("LanceDB get_pinned failed: %s", e)
            return []

    def delete(self, memory_id: str) -> bool:
        """Delete a memory by ID. Returns True if deleted."""
        self._ensure_ready()
        if not re.match(r'^[a-zA-Z0-9_-]+$', str(memory_id)):
            log.warning("lance_store: rejected unsafe memory_id in delete: %r", memory_id)
            return False
        try:
            self._table.delete(f"id = '{memory_id}'")
            return True
        except Exception as e:
            log.warning("LanceDB delete failed id=%s: %s", memory_id, e)
            return False

    def count(self) -> int:
        self._ensure_ready()
        return self._table.count_rows()

    def get_all_ids(self) -> set[str]:
        """Return the set of memory ids currently in the vector table."""
        self._ensure_ready()
        try:
            tbl = self._table.to_arrow()
            return set(tbl.column("id").to_pylist())
        except Exception as e:
            log.warning("LanceDB get_all_ids failed: %s", e)
            return set()

    @property
    def is_ready(self) -> bool:
        return self._ready


def migrate_from_sqlite(model) -> int:
    """One-shot migration: copy Memory rows from SQLite into LanceDB.

    Args:
        model: The Memory SQLAlchemy model class (passed in to avoid circular imports).

    Returns:
        Number of records migrated.
    """
    from app.services.memory import get_memory_service
    store = get_lance_store()
    svc = get_memory_service()

    memories = model.query.all()
    migrated = 0

    for m in memories:
        if not m.embedding:
            continue
        try:
            vec = np.frombuffer(m.embedding, dtype=np.float32).tolist()
            store.add({
                "id":                 m.id,
                "title":              m.title,
                "content":            m.content,
                "memory_type":        m.memory_type,
                "source":             m.source,
                "pinned":             m.pinned,
                "scope_project_id":   m.scope_project_id or "",
                "scope_epic_id":      m.scope_epic_id or "",
                "scope_task_id":      m.scope_task_id or "",
                "scope_connector_id": m.scope_connector_id or "",
                "created_at":         m.created_at.isoformat() if m.created_at else "",
            }, vec)
            migrated += 1
        except Exception as e:
            log.warning("migrate: skipped id=%s error=%s", m.id, e)

    log.info("LanceDB migration complete: %d/%d records", migrated, len(memories))
    return migrated


def reconcile_from_sqlite(model) -> int:
    """Add any SQLite memory (with a stored embedding) missing from LanceDB.

    Heals divergence caused by a LanceDB write that failed AFTER the SQLite
    commit — such a memory would otherwise never be searchable. Vectors are
    rebuilt from the stored embedding bytes, so no re-embedding is needed.

    Returns the number of records re-indexed.
    """
    store = get_lance_store()
    existing_ids = store.get_all_ids()

    added = 0
    for m in model.query.all():
        if m.id in existing_ids or not m.embedding:
            continue
        try:
            vec = np.frombuffer(m.embedding, dtype=np.float32).tolist()
            store.add({
                "id":                 m.id,
                "title":              m.title,
                "content":            m.content,
                "memory_type":        m.memory_type,
                "source":             m.source,
                "pinned":             m.pinned,
                "scope_project_id":   m.scope_project_id or "",
                "scope_epic_id":      m.scope_epic_id or "",
                "scope_task_id":      m.scope_task_id or "",
                "scope_connector_id": m.scope_connector_id or "",
                "created_at":         m.created_at.isoformat() if m.created_at else "",
            }, vec)
            added += 1
        except Exception as e:
            log.warning("reconcile: skipped id=%s error=%s", m.id, e)

    if added:
        log.info("LanceDB reconcile: re-indexed %d missing records", added)
    return added
