"""
Orion's Belt — LLM usage/cost dashboard.

Reads LLMLog, the choke-point log every LLM call writes to (see
app/services/llm.py::_call_llm_sync). Pricing is admin-configured per model
under the Setting key "llm.model_pricing" (a JSON string, not a value_type
"json" Setting — see app/services/llm.py::_estimate_llm_cost for why: this
lets both sides use the exact same plain json.loads/json.dumps round trip).
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request

from app.models.logs import LLMLog
from app.models.settings import Setting

bp = Blueprint("usage", __name__)
log = logging.getLogger("orions-belt")


@bp.route("/usage")
@bp.route("/usage/")
def index():
    return render_template("usage.html")


@bp.route("/api/usage/summary", methods=["GET"])
def usage_summary():
    """Token/cost totals over the last `days` days (default 30, max 365),
    broken down by day, by model, and by agent (LLMLog.run_id — for
    room/goal-pursuit calls this is the agent id, see chat_rooms.py)."""
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = LLMLog.query.filter(LLMLog.created_at >= since).all()

    totals = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0, "errors": 0}
    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}

    # Zero-fill every day in the window so the chart doesn't have gaps.
    for i in range(days):
        day = (since + timedelta(days=i)).strftime("%Y-%m-%d")
        by_day[day] = {"day": day, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}

    agent_names = {}
    try:
        from app.models.agent import Agent
        agent_names = {a.id: a.name for a in Agent.query.all()}
    except Exception:
        pass

    for r in rows:
        totals["tokens_in"] += r.tokens_in or 0
        totals["tokens_out"] += r.tokens_out or 0
        totals["cost_usd"] += r.estimated_cost_usd or 0.0
        totals["calls"] += 1
        if not r.success:
            totals["errors"] += 1

        day = r.created_at.strftime("%Y-%m-%d") if r.created_at else "?"
        d = by_day.setdefault(day, {"day": day, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0})
        d["tokens_in"] += r.tokens_in or 0
        d["tokens_out"] += r.tokens_out or 0
        d["cost_usd"] += r.estimated_cost_usd or 0.0
        d["calls"] += 1

        model = r.model or "(unknown)"
        m = by_model.setdefault(model, {"model": model, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0})
        m["tokens_in"] += r.tokens_in or 0
        m["tokens_out"] += r.tokens_out or 0
        m["cost_usd"] += r.estimated_cost_usd or 0.0
        m["calls"] += 1

        agent_id = r.run_id or "(unattributed)"
        a = by_agent.setdefault(agent_id, {
            "agent_id": agent_id,
            "agent_name": agent_names.get(agent_id, "Unattributed" if agent_id == "(unattributed)" else agent_id),
            "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0,
        })
        a["tokens_in"] += r.tokens_in or 0
        a["tokens_out"] += r.tokens_out or 0
        a["cost_usd"] += r.estimated_cost_usd or 0.0
        a["calls"] += 1

    return jsonify({
        "days": days,
        "totals": totals,
        "by_day": sorted(by_day.values(), key=lambda d: d["day"]),
        "by_model": sorted(by_model.values(), key=lambda m: -m["tokens_in"] - m["tokens_out"]),
        "by_agent": sorted(by_agent.values(), key=lambda a: -a["tokens_in"] - a["tokens_out"]),
    })


@bp.route("/api/usage/pricing", methods=["GET"])
def get_pricing():
    """Admin-configured per-model $/1M-token pricing."""
    raw = Setting.get("llm.model_pricing")
    try:
        pricing = json.loads(raw) if raw else {}
    except Exception:
        pricing = {}
    return jsonify(pricing)


@bp.route("/api/usage/pricing", methods=["PUT"])
def set_pricing():
    """Replace the whole pricing map. Body: {model: {input_per_1m, output_per_1m}}."""
    body = request.get_json()
    if not isinstance(body, dict):
        return jsonify({"error": "Expected a JSON object of {model: {input_per_1m, output_per_1m}}"}), 400
    for model, entry in body.items():
        if not isinstance(entry, dict):
            return jsonify({"error": f"Pricing entry for '{model}' must be an object"}), 400
        for k in ("input_per_1m", "output_per_1m"):
            if k in entry and entry[k] is not None:
                try:
                    float(entry[k])
                except (TypeError, ValueError):
                    return jsonify({"error": f"'{model}'.{k} must be a number"}), 400
    Setting.set("llm.model_pricing", json.dumps(body), value_type="string")
    return jsonify({"success": True, "pricing": body})
