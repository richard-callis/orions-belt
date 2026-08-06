import csv
import io
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request, make_response
from sqlalchemy import or_

from app import db
from app.models.logs import AuditLog, PIILog, AgentLog, LLMLog

bp = Blueprint("logs", __name__, url_prefix="/logs")

_RANGE_HOURS = {"1h": 1, "24h": 24, "7d": 168}


def _since(range_str):
    hours = _RANGE_HOURS.get(range_str)
    if hours is None:
        return None
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _audit_query(q, since, limit):
    query = AuditLog.query
    if since:
        query = query.filter(AuditLog.created_at >= since)
    if q:
        query = query.filter(AuditLog.tool_name.ilike(f"%{q}%"))
    return query.order_by(AuditLog.created_at.desc()).limit(limit).all()


def _audit_shape(row):
    detail = row.result_summary or row.error or row.input_summary or ""
    return {
        "id": row.id,
        "time": row.created_at.isoformat() if row.created_at else None,
        "tool": row.tool_name,
        "tier": row.tier,
        "caller": row.caller or "",
        "outcome": row.outcome,
        "detail": detail,
    }


def _pii_query(q, since, limit):
    query = PIILog.query
    if since:
        query = query.filter(PIILog.created_at >= since)
    if q:
        query = query.filter(PIILog.entity_types.ilike(f"%{q}%"))
    return query.order_by(PIILog.created_at.desc()).limit(limit).all()


def _pii_shape(row):
    return {
        "id": row.id,
        "time": row.created_at.isoformat() if row.created_at else None,
        "session_id": row.session_id or "",
        "direction": row.direction or "outbound",
        "entities_found": row.entities_found or 0,
        "entity_types": row.entity_types or "",
        "detection_sources": row.detection_sources or "",
    }


def _agent_query(q, since, limit):
    query = AgentLog.query
    if since:
        query = query.filter(AgentLog.created_at >= since)
    if q:
        query = query.filter(AgentLog.agent_name.ilike(f"%{q}%"))
    return query.order_by(AgentLog.created_at.desc()).limit(limit).all()


def _agent_shape(row):
    return {
        "id": row.id,
        "time": row.created_at.isoformat() if row.created_at else None,
        "run_id": row.run_id or "",
        "agent_name": row.agent_name or "",
        "event": row.event or "",
        "detail": row.detail or "",
        "tokens_used": row.tokens_used or 0,
    }


def _llm_query(q, since, limit):
    query = LLMLog.query
    if since:
        query = query.filter(LLMLog.created_at >= since)
    if q:
        query = query.filter(
            or_(LLMLog.model.ilike(f"%{q}%"), LLMLog.provider.ilike(f"%{q}%"))
        )
    return query.order_by(LLMLog.created_at.desc()).limit(limit).all()


def _llm_shape(row):
    return {
        "id": row.id,
        "time": row.created_at.isoformat() if row.created_at else None,
        "provider": row.provider or "",
        "model": row.model or "",
        "tokens_in": row.tokens_in or 0,
        "tokens_out": row.tokens_out or 0,
        "latency_ms": row.latency_ms or 0,
        "estimated_cost_usd": row.estimated_cost_usd,
        "success": row.success,
        "error": row.error or "",
        # Captured only when "LLM Debug Logging" was on for this call — lets
        # the UI show a "view traffic" affordance without a separate request
        # per row, without shipping the (potentially large) payloads
        # themselves in the list view.
        "has_traffic_capture": bool(row.request_json or row.response_json),
    }


def _audit_stats(since):
    query = AuditLog.query
    if since:
        query = query.filter(AuditLog.created_at >= since)
    rows = query.with_entities(AuditLog.outcome).all()
    total = len(rows)
    counts = {"auto": 0, "approved": 0, "rejected": 0, "blocked": 0}
    for (outcome,) in rows:
        if outcome in counts:
            counts[outcome] += 1
    return {"total": total, **counts}


def _fetch_entries(stream, q, range_str, limit):
    since = _since(range_str)
    limit = min(int(limit), 500)
    if stream == "audit":
        rows = _audit_query(q, since, limit)
        entries = [_audit_shape(r) for r in rows]
        stats = _audit_stats(since)
    elif stream == "pii":
        rows = _pii_query(q, since, limit)
        entries = [_pii_shape(r) for r in rows]
        stats = {"total": len(entries), "auto": 0, "approved": 0, "rejected": 0, "blocked": 0}
    elif stream == "agent":
        rows = _agent_query(q, since, limit)
        entries = [_agent_shape(r) for r in rows]
        stats = {"total": len(entries), "auto": 0, "approved": 0, "rejected": 0, "blocked": 0}
    elif stream == "llm":
        rows = _llm_query(q, since, limit)
        entries = [_llm_shape(r) for r in rows]
        stats = {"total": len(entries), "auto": 0, "approved": 0, "rejected": 0, "blocked": 0}
    else:
        return None, None
    return entries, stats


@bp.route("/")
@bp.route("")
def index():
    return render_template("logs.html")


@bp.route("/api/logs")
def api_logs():
    stream = request.args.get("stream", "")
    q = request.args.get("q", "").strip()
    range_str = request.args.get("range", "24h")
    try:
        limit = min(int(request.args.get("limit", 100)), 1000)
    except (ValueError, TypeError):
        return jsonify({"error": "limit must be an integer"}), 400

    entries, stats = _fetch_entries(stream, q or None, range_str, limit)
    if entries is None:
        return jsonify({"error": "invalid stream"}), 400

    return jsonify({"entries": entries, "stats": stats})


@bp.route("/api/logs/export")
def api_logs_export():
    stream = request.args.get("stream", "")
    q = request.args.get("q", "").strip()
    range_str = request.args.get("range", "24h")
    try:
        limit = min(int(request.args.get("limit", 500)), 5000)
    except (ValueError, TypeError):
        return jsonify({"error": "limit must be an integer"}), 400

    entries, _ = _fetch_entries(stream, q or None, range_str, limit)
    if entries is None:
        return jsonify({"error": "invalid stream"}), 400

    buf = io.StringIO()
    if entries:
        writer = csv.DictWriter(buf, fieldnames=entries[0].keys())
        writer.writeheader()
        writer.writerows(entries)
    else:
        buf.write("")

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"logs_{stream}_{date_str}.csv"
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


def _llm_detail_shape(row):
    """Full LLM log row including parsed (not re-encoded) request/response
    JSON — separate from _llm_shape, which the list view uses and
    deliberately omits these potentially-large payloads."""

    def _parse(raw):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw  # not valid JSON (shouldn't happen) — return as text rather than drop it

    d = _llm_shape(row)
    d["request_json"] = _parse(row.request_json)
    d["response_json"] = _parse(row.response_json)
    return d


@bp.route("/api/logs/llm/<log_id>")
def api_logs_llm_detail(log_id):
    """Full detail for one LLM call, including captured request/response
    JSON if "LLM Debug Logging" was on when it was made."""
    row = LLMLog.query.get(log_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_llm_detail_shape(row))


@bp.route("/api/logs/llm/<log_id>/download")
def api_logs_llm_download(log_id):
    """Download one LLM call's captured request/response as a JSON file."""
    row = LLMLog.query.get(log_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    if not (row.request_json or row.response_json):
        return jsonify({"error": "No traffic captured for this call — enable "
                                  "\"LLM Debug Logging\" in Settings before it runs again"}), 404

    body = json.dumps(_llm_detail_shape(row), indent=2)
    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = f"attachment; filename=llm_call_{log_id}.json"
    return resp


@bp.route("/api/logs/llm/export")
def api_logs_llm_export():
    """Bulk-download captured LLM request/response JSON for the current
    filter — the CSV export above can't usefully hold nested JSON payloads,
    so this is a separate JSON-array download covering only rows that
    actually have a capture (debug logging must have been on when they ran)."""
    q = request.args.get("q", "").strip()
    range_str = request.args.get("range", "24h")
    try:
        limit = min(int(request.args.get("limit", 500)), 5000)
    except (ValueError, TypeError):
        return jsonify({"error": "limit must be an integer"}), 400

    since = _since(range_str)
    query = LLMLog.query.filter(
        or_(LLMLog.request_json.isnot(None), LLMLog.response_json.isnot(None))
    )
    if since:
        query = query.filter(LLMLog.created_at >= since)
    if q:
        query = query.filter(or_(LLMLog.model.ilike(f"%{q}%"), LLMLog.provider.ilike(f"%{q}%")))
    rows = query.order_by(LLMLog.created_at.desc()).limit(limit).all()

    body = json.dumps([_llm_detail_shape(r) for r in rows], indent=2)

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = f"attachment; filename=llm_traffic_{date_str}.json"
    return resp


@bp.route("/api/logs/audit/verify")
def api_audit_verify():
    """Verify the audit log's hash chain — detects tampering with or
    deletion of AuditLog rows after they were written. See
    app/services/audit_chain.py for how the chain itself works."""
    from app.services.audit_chain import verify_chain
    return jsonify(verify_chain())
