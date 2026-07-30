"""
Tests for the PII false-positive exceptions allowlist and single-token
reveal endpoint.
"""
from app import db
from app.models.pii import PIIException, PIIHashEntry
from app.services.crypto import encrypt_data
from app.services.pii_guard import _filter_exceptions, _regex_match_for_exception


def _make_hash_entry(hash_token, entity_type, value, occurrence_count=1):
    """Creates a PIIHashEntry and returns its id (not the ORM object — the
    caller's app_context exits before cleanup runs, which would otherwise
    detach the instance)."""
    entry = PIIHashEntry(
        hash_token=hash_token,
        full_hash="f" * 64,
        original_value=encrypt_data(value),
        entity_type=entity_type,
        detection_source="presidio",
        occurrence_count=occurrence_count,
    )
    db.session.add(entry)
    db.session.commit()
    return entry.id


class TestFilterExceptions:
    def test_exact_match_drops_span(self, app):
        with app.app_context():
            exc = PIIException(entity_type="PERSON", match_mode="exact", value=encrypt_data("Orion's Belt"))
            db.session.add(exc)
            db.session.commit()
            try:
                spans = [(0, 12, "PERSON", "Orion's Belt", "gliner")]
                assert _filter_exceptions(spans) == []
            finally:
                PIIException.query.filter_by(id=exc.id).delete()
                db.session.commit()

    def test_exact_match_is_case_sensitive(self, app):
        with app.app_context():
            exc = PIIException(entity_type="PERSON", match_mode="exact", value=encrypt_data("Orion's Belt"))
            db.session.add(exc)
            db.session.commit()
            try:
                spans = [(0, 12, "PERSON", "orion's belt", "gliner")]
                assert _filter_exceptions(spans) == spans  # different case — not excepted
            finally:
                PIIException.query.filter_by(id=exc.id).delete()
                db.session.commit()

    def test_normalized_match_ignores_case_and_whitespace(self, app):
        with app.app_context():
            exc = PIIException(entity_type="PERSON", match_mode="normalized", value=encrypt_data("Orion's  Belt"))
            db.session.add(exc)
            db.session.commit()
            try:
                spans = [(0, 11, "PERSON", "orion's belt", "gliner")]
                assert _filter_exceptions(spans) == []
            finally:
                PIIException.query.filter_by(id=exc.id).delete()
                db.session.commit()

    def test_different_entity_type_not_excepted(self, app):
        with app.app_context():
            exc = PIIException(entity_type="PERSON", match_mode="exact", value=encrypt_data("Orion"))
            db.session.add(exc)
            db.session.commit()
            try:
                spans = [(0, 5, "ORG", "Orion", "gliner")]
                assert _filter_exceptions(spans) == spans
            finally:
                PIIException.query.filter_by(id=exc.id).delete()
                db.session.commit()

    def test_no_exceptions_is_noop(self, app):
        with app.app_context():
            spans = [(0, 5, "PERSON", "Alice", "gliner")]
            assert _filter_exceptions(spans) == spans

    def test_empty_spans_is_noop(self, app):
        with app.app_context():
            assert _filter_exceptions([]) == []

    def test_regex_match_drops_span(self, app):
        with app.app_context():
            exc = PIIException(entity_type="PHONE", match_mode="regex", value=encrypt_data(r"^555-\d{3}-\d{4}$"))
            db.session.add(exc)
            db.session.commit()
            try:
                spans = [(0, 12, "PHONE", "555-123-4567", "regex")]
                assert _filter_exceptions(spans) == []
            finally:
                PIIException.query.filter_by(id=exc.id).delete()
                db.session.commit()

    def test_regex_no_match_keeps_span(self, app):
        with app.app_context():
            exc = PIIException(entity_type="PHONE", match_mode="regex", value=encrypt_data(r"^555-\d{3}-\d{4}$"))
            db.session.add(exc)
            db.session.commit()
            try:
                spans = [(0, 12, "PHONE", "212-555-9999", "regex")]
                assert _filter_exceptions(spans) == spans
            finally:
                PIIException.query.filter_by(id=exc.id).delete()
                db.session.commit()


class TestRegexMatchForException:
    def test_matches_simple_pattern(self):
        assert _regex_match_for_exception(r"Orion.*", "Orion's Belt") is True

    def test_no_match_returns_false(self):
        assert _regex_match_for_exception(r"^Zeta", "Orion's Belt") is False

    def test_is_a_fullmatch_not_an_unanchored_substring_search(self):
        # A pattern must match the ENTIRE value, not just appear somewhere in
        # it — otherwise a narrow exception like "555-1234" would also
        # exempt "x555-1234x", or worse, a different value that merely
        # contains it.
        assert _regex_match_for_exception(r"555-1234", "x555-1234x") is False
        assert _regex_match_for_exception(r"555-1234", "555-1234") is True

    def test_invalid_pattern_fails_safe(self):
        # Unbalanced parenthesis — a compile error, not a crash.
        assert _regex_match_for_exception(r"(unclosed", "anything") is False

    def test_catastrophic_pattern_times_out_and_fails_safe(self):
        # (a|a)* against a non-matching string is the textbook ReDoS shape —
        # must return False (fail open, not hang or raise) well under a second.
        import time
        start = time.time()
        result = _regex_match_for_exception(r"(a|a)*$", "a" * 40 + "!")
        elapsed = time.time() - start
        assert result is False
        assert elapsed < 1.0

    def test_oversized_pattern_rejected_without_running(self):
        assert _regex_match_for_exception("a" * 500, "a") is False

    def test_oversized_value_rejected_without_running(self):
        assert _regex_match_for_exception("a", "a" * 500) is False


class TestPIIExceptionModel:
    def test_to_dict_decrypts_value(self, app):
        with app.app_context():
            exc = PIIException(entity_type="EMAIL", match_mode="exact", value=encrypt_data("test@example.com"))
            db.session.add(exc)
            db.session.commit()
            try:
                d = exc.to_dict()
                assert d["value"] == "test@example.com"
                assert d["entity_type"] == "EMAIL"
            finally:
                PIIException.query.filter_by(id=exc.id).delete()
                db.session.commit()


class TestPiiHashesRoute:
    def test_list_hashes_never_returns_decrypted_value(self, app, client):
        with app.app_context():
            entry_id = _make_hash_entry("abc12345", "PERSON", "Alice Smith")
        try:
            resp = client.get("/api/pii/hashes")
            assert resp.status_code == 200
            data = resp.get_json()
            row = next(r for r in data if r["hash_token"] == "abc12345")
            assert "value" not in row
            assert row["entity_type"] == "PERSON"
        finally:
            with app.app_context():
                PIIHashEntry.query.filter_by(id=entry_id).delete()
                db.session.commit()


class TestPiiRevealRoute:
    def test_reveal_known_token(self, app, client):
        with app.app_context():
            entry_id = _make_hash_entry("reveal01", "EMAIL", "secret@example.com")
        try:
            resp = client.get("/api/pii/reveal/reveal01")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["value"] == "secret@example.com"
            assert data["entity_type"] == "EMAIL"
        finally:
            with app.app_context():
                PIIHashEntry.query.filter_by(id=entry_id).delete()
                db.session.commit()

    def test_reveal_unknown_token_404s(self, app, client):
        resp = client.get("/api/pii/reveal/doesnotexist")
        assert resp.status_code == 404


class TestPiiExceptionsRoutes:
    def test_create_requires_existing_hash_token(self, app, client):
        # Anti-poisoning: an exception can't be created from arbitrary text —
        # only by referencing a real prior detection.
        resp = client.post("/api/pii/exceptions", json={
            "hash_token": "not-a-real-token", "match_mode": "exact",
        })
        assert resp.status_code == 404

    def test_create_from_real_detection(self, app, client):
        with app.app_context():
            entry_id = _make_hash_entry("mkexc001", "ORG", "Orion's Belt")
        try:
            resp = client.post("/api/pii/exceptions", json={
                "hash_token": "mkexc001", "match_mode": "normalized",
            })
            assert resp.status_code == 201
            data = resp.get_json()
            assert data["entity_type"] == "ORG"
            assert data["value"] == "Orion's Belt"
            assert data["source_hash_token"] == "mkexc001"

            list_resp = client.get("/api/pii/exceptions")
            assert any(e["id"] == data["id"] for e in list_resp.get_json())

            del_resp = client.delete(f"/api/pii/exceptions/{data['id']}")
            assert del_resp.status_code == 204
            list_resp2 = client.get("/api/pii/exceptions")
            assert not any(e["id"] == data["id"] for e in list_resp2.get_json())
        finally:
            with app.app_context():
                PIIHashEntry.query.filter_by(id=entry_id).delete()
                PIIException.query.filter_by(source_hash_token="mkexc001").delete()
                db.session.commit()

    def test_create_rejects_invalid_match_mode(self, app, client):
        with app.app_context():
            entry_id = _make_hash_entry("badmode1", "PERSON", "Bob")
        try:
            resp = client.post("/api/pii/exceptions", json={
                "hash_token": "badmode1", "match_mode": "fuzzy",
            })
            assert resp.status_code == 400
        finally:
            with app.app_context():
                PIIHashEntry.query.filter_by(id=entry_id).delete()
                db.session.commit()

    def test_create_rejects_regex_mode_when_detected_text_does_not_compile(self, app, client):
        # The detected literal text becomes the pattern verbatim (never
        # hand-authored) — something like an unbalanced paren in a phone
        # extension is a totally ordinary detected value but invalid regex
        # syntax. Reject at creation time rather than silently never apply.
        with app.app_context():
            entry_id = _make_hash_entry("badregex1", "PHONE", "555 (ext. 123")
        try:
            resp = client.post("/api/pii/exceptions", json={
                "hash_token": "badregex1", "match_mode": "regex",
            })
            assert resp.status_code == 400
            assert "valid regex" in resp.get_json()["error"]
        finally:
            with app.app_context():
                PIIHashEntry.query.filter_by(id=entry_id).delete()
                db.session.commit()

    def test_create_accepts_regex_match_mode(self, app, client):
        with app.app_context():
            entry_id = _make_hash_entry("regexok1", "PHONE", "555-123-4567")
        try:
            resp = client.post("/api/pii/exceptions", json={
                "hash_token": "regexok1", "match_mode": "regex",
            })
            assert resp.status_code == 201
            data = resp.get_json()
            assert data["match_mode"] == "regex"
            assert data["value"] == "555-123-4567"
        finally:
            with app.app_context():
                PIIHashEntry.query.filter_by(id=entry_id).delete()
                PIIException.query.filter_by(source_hash_token="regexok1").delete()
                db.session.commit()
