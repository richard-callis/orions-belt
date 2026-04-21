import csv
import io
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
    limit = request.args.get("limit", 100)

    entries, stats = _fetch_entries(stream, q or None, range_str, limit)
    if entries is None:
        return jsonify({"error": "invalid stream"}), 400

    return jsonify({"entries": entries, "stats": stats})


@bp.route("/api/logs/export")
def api_logs_export():
    stream = request.args.get("stream", "")
    q = request.args.get("q", "").strip()
    range_str = request.args.get("range", "24h")
    limit = request.args.get("limit", 500)

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
