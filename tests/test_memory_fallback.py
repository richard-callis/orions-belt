"""
Tests for the NumPy cosine-similarity memory fallback used when LanceDB is
unavailable (e.g. not installed on Windows).
"""
import numpy as np

from app import db
from app.models.memory import Memory
from app.services.memory import get_memory_service


def _mem(mid, vec):
    return Memory(
        id=mid,
        memory_type="persistent",
        title=mid,
        content="content",
        source="user",
        embedding=np.asarray(vec, dtype="float32").tobytes(),
    )


class TestNumpyMemoryFallback:
    def test_orders_by_cosine_similarity(self, app):
        with app.app_context():
            db.session.add_all([
                _mem("m-close", [1.0, 0.0, 0.0]),   # aligned with query
                _mem("m-mid",   [0.7, 0.7, 0.0]),   # 45°
                _mem("m-far",   [0.0, 1.0, 0.0]),   # orthogonal
            ])
            db.session.commit()
            try:
                svc = get_memory_service()
                results = svc._semantic_search_sqlite([1.0, 0.0, 0.0], k=3)
                ids = [m.id for m in results]
                assert ids[0] == "m-close"
                assert ids.index("m-mid") < ids.index("m-far")
            finally:
                Memory.query.filter(
                    Memory.id.in_(["m-close", "m-mid", "m-far"])
                ).delete(synchronize_session=False)
                db.session.commit()

    def test_respects_top_k(self, app):
        with app.app_context():
            db.session.add_all([_mem(f"k{i}", [1.0, i * 0.1, 0.0]) for i in range(5)])
            db.session.commit()
            try:
                svc = get_memory_service()
                assert len(svc._semantic_search_sqlite([1.0, 0.0, 0.0], k=2)) == 2
            finally:
                Memory.query.filter(
                    Memory.id.in_([f"k{i}" for i in range(5)])
                ).delete(synchronize_session=False)
                db.session.commit()

    def test_skips_dimension_mismatch(self, app):
        with app.app_context():
            db.session.add_all([
                _mem("good", [1.0, 0.0, 0.0]),
                _mem("wrongdim", [1.0, 0.0]),   # 2-dim, query is 3-dim
            ])
            db.session.commit()
            try:
                svc = get_memory_service()
                results = svc._semantic_search_sqlite([1.0, 0.0, 0.0], k=5)
                ids = [m.id for m in results]
                assert "good" in ids
                assert "wrongdim" not in ids   # mismatched dim is skipped, not a crash
            finally:
                Memory.query.filter(
                    Memory.id.in_(["good", "wrongdim"])
                ).delete(synchronize_session=False)
                db.session.commit()

    def test_empty_store_returns_empty(self, app):
        with app.app_context():
            svc = get_memory_service()
            assert svc._semantic_search_sqlite([1.0, 0.0, 0.0], k=5) == []
