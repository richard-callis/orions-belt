"""
Tests for the LLM-traffic detail/download/export routes in app/routes/logs.py
— GET /logs/api/logs/llm/<id>, /<id>/download, and /export. These only
return anything for rows with captured request_json/response_json (i.e.
"LLM Debug Logging" was on when the call ran) — everything else 404s or is
excluded from the export, rather than silently returning empty payloads.
"""
import json

from app import db
from app.models.logs import LLMLog


def _make_log(app, **overrides):
    with app.app_context():
        row = LLMLog(
            provider="openai", model="gpt-4o", tokens_in=10, tokens_out=5,
            latency_ms=100, success=True,
            **overrides,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


class TestLlmDetailRoute:
    def test_404_for_missing_row(self, client):
        resp = client.get("/logs/api/logs/llm/does-not-exist")
        assert resp.status_code == 404

    def test_returns_parsed_request_and_response(self, app, client):
        log_id = _make_log(
            app,
            request_json=json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}),
            response_json=json.dumps({"choices": [{"message": {"content": "hello"}}]}),
        )
        try:
            resp = client.get(f"/logs/api/logs/llm/{log_id}")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["request_json"]["model"] == "gpt-4o"
            assert data["response_json"]["choices"][0]["message"]["content"] == "hello"
            assert data["has_traffic_capture"] is True
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id=log_id).delete()
                db.session.commit()

    def test_null_when_no_capture(self, app, client):
        log_id = _make_log(app)
        try:
            resp = client.get(f"/logs/api/logs/llm/{log_id}")
            data = resp.get_json()
            assert data["request_json"] is None
            assert data["response_json"] is None
            assert data["has_traffic_capture"] is False
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id=log_id).delete()
                db.session.commit()


class TestLlmDownloadRoute:
    def test_404_for_missing_row(self, client):
        resp = client.get("/logs/api/logs/llm/does-not-exist/download")
        assert resp.status_code == 404

    def test_404_when_no_capture(self, app, client):
        log_id = _make_log(app)
        try:
            resp = client.get(f"/logs/api/logs/llm/{log_id}/download")
            assert resp.status_code == 404
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id=log_id).delete()
                db.session.commit()

    def test_downloads_json_file_with_captured_traffic(self, app, client):
        log_id = _make_log(
            app,
            request_json=json.dumps({"model": "gpt-4o"}),
            response_json=json.dumps({"id": "resp-1"}),
        )
        try:
            resp = client.get(f"/logs/api/logs/llm/{log_id}/download")
            assert resp.status_code == 200
            assert resp.headers["Content-Type"] == "application/json"
            assert f"llm_call_{log_id}.json" in resp.headers["Content-Disposition"]
            body = json.loads(resp.data)
            assert body["request_json"]["model"] == "gpt-4o"
            assert body["response_json"]["id"] == "resp-1"
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id=log_id).delete()
                db.session.commit()


class TestLlmExportRoute:
    def test_only_includes_rows_with_captured_traffic(self, app, client):
        with_capture = _make_log(app, request_json=json.dumps({"a": 1}))
        without_capture = _make_log(app)
        try:
            resp = client.get("/logs/api/logs/llm/export?range=all")
            assert resp.status_code == 200
            assert resp.headers["Content-Type"] == "application/json"
            body = json.loads(resp.data)
            ids = {row["id"] for row in body}
            assert with_capture in ids
            assert without_capture not in ids
        finally:
            with app.app_context():
                LLMLog.query.filter_by(id=with_capture).delete()
                LLMLog.query.filter_by(id=without_capture).delete()
                db.session.commit()

    def test_empty_export_is_valid_json_array(self, client):
        resp = client.get("/logs/api/logs/llm/export?range=1h&q=zzz-no-match-zzz")
        assert resp.status_code == 200
        assert json.loads(resp.data) == []

    def test_invalid_limit_returns_400(self, client):
        resp = client.get("/logs/api/logs/llm/export?limit=notanumber")
        assert resp.status_code == 400
