"""
Orion's Belt — LLM usage/cost dashboard.

Reads LLMLog, the choke-point log every LLM call writes to (see
app/services/llm.py::_call_llm_sync and the per-turn commit points in
app/routes/chat.py). Pricing is admin-configured per model under the
Setting key "llm.model_pricing" (a JSON string, not a value_type "json"
Setting — see app/services/llm.py::_estimate_llm_cost_and_savings for why:
this lets both sides use the exact same plain json.loads/json.dumps round
trip).

Mirrors orion-web's /api/cost/summary cost/savings split: a self-hosted
model's $ value is money avoided, not money spent, so it's tracked as
"savings" rather than folded into "cost" (which would misrepresent actual
spend) or discarded (which would hide the value self-hosting provides).
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request

from app.models.logs import LLMLog
from app.models.settings import Setting

bp = Blueprint("usage", __name__)
log = logging.getLogger("orions-belt")


def _load_pricing() -> dict:
    raw = Setting.get("llm.model_pricing")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


@bp.route("/usage")
@bp.route("/usage/")
def index():
    return render_template("usage.html")


@bp.route("/api/usage/summary", methods=["GET"])
def usage_summary():
    """Token/cost/savings totals over the last `days` days (default 30, max
    365), broken down by day, by model, and by agent (LLMLog.run_id — for
    room/goal-pursuit calls this is the agent id, see chat_rooms.py). Each
    agent also carries a `last7` sparkline: total tokens per day for the
    last 7 days of the window, mirroring orion-web's /cost page."""
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))

    # Anchor to UTC midnight so the zero-filled day-key window actually ends
    # on today — datetime.now() carries a fractional time-of-day, so
    # `since = now - timedelta(days=days)` shifts every bucket key off by
    # however many hours have elapsed today, and the last bucket silently
    # stops being "today" (a record created right now wouldn't land in any
    # zero-filled key, e.g. the by-agent last7 sparkline showing all zeros).
    today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    since = today_midnight - timedelta(days=days - 1)
    rows = LLMLog.query.filter(LLMLog.created_at >= since).all()

    totals = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "savings_usd": 0.0, "calls": 0, "errors": 0}
    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    agent_day_tokens: dict[str, dict[str, int]] = {}

    # Zero-fill every day in the window so the chart doesn't have gaps.
    day_keys = []
    for i in range(days):
        day = (since + timedelta(days=i)).strftime("%Y-%m-%d")
        day_keys.append(day)
        by_day[day] = {"day": day, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "savings_usd": 0.0, "calls": 0}

    agent_names = {}
    try:
        from app.models.agent import Agent
        agent_names = {a.id: a.name for a in Agent.query.all()}
    except Exception:
        pass

    for r in rows:
        cost = r.estimated_cost_usd or 0.0
        savings = r.estimated_savings_usd or 0.0
        day = r.created_at.strftime("%Y-%m-%d") if r.created_at else "?"

        totals["tokens_in"] += r.tokens_in or 0
        totals["tokens_out"] += r.tokens_out or 0
        totals["cost_usd"] += cost
        totals["savings_usd"] += savings
        totals["calls"] += 1
        if not r.success:
            totals["errors"] += 1

        d = by_day.setdefault(day, {"day": day, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "savings_usd": 0.0, "calls": 0})
        d["tokens_in"] += r.tokens_in or 0
        d["tokens_out"] += r.tokens_out or 0
        d["cost_usd"] += cost
        d["savings_usd"] += savings
        d["calls"] += 1

        model = r.model or "(unknown)"
        m = by_model.setdefault(model, {
            "model": model,
            "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "savings_usd": 0.0, "calls": 0,
        })
        m["tokens_in"] += r.tokens_in or 0
        m["tokens_out"] += r.tokens_out or 0
        m["cost_usd"] += cost
        m["savings_usd"] += savings
        m["calls"] += 1

        agent_id = r.run_id or "(unattributed)"
        a = by_agent.setdefault(agent_id, {
            "agent_id": agent_id,
            "agent_name": agent_names.get(agent_id, "Unattributed" if agent_id == "(unattributed)" else agent_id),
            "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "savings_usd": 0.0, "calls": 0,
        })
        a["tokens_in"] += r.tokens_in or 0
        a["tokens_out"] += r.tokens_out or 0
        a["cost_usd"] += cost
        a["savings_usd"] += savings
        a["calls"] += 1

        agent_days = agent_day_tokens.setdefault(agent_id, {})
        agent_days[day] = agent_days.get(day, 0) + (r.tokens_in or 0) + (r.tokens_out or 0)

    last7_keys = day_keys[-7:]
    for agent_id, a in by_agent.items():
        agent_days = agent_day_tokens.get(agent_id, {})
        a["last7"] = [agent_days.get(k, 0) for k in last7_keys]

    # Derive the self-hosted badge from the $ actually recorded per-row
    # (frozen at write time via estimated_cost_usd/savings_usd), not a
    # live lookup against current pricing config — a model whose
    # self_hosted flag changed after some rows were written would
    # otherwise show a badge that contradicts its own cost/savings split
    # in the same response (e.g. "self-hosted" next to real $ spend).
    for m in by_model.values():
        m["self_hosted"] = m["savings_usd"] > 0 and m["cost_usd"] == 0

    return jsonify({
        "days": days,
        "totals": totals,
        "by_day": sorted(by_day.values(), key=lambda d: d["day"]),
        "by_model": sorted(by_model.values(), key=lambda m: -m["tokens_in"] - m["tokens_out"]),
        "by_agent": sorted(by_agent.values(), key=lambda a: -a["tokens_in"] - a["tokens_out"]),
    })


@bp.route("/api/usage/pricing", methods=["GET"])
def get_pricing():
    """Admin-configured per-model $/1M-token pricing + self_hosted flag."""
    return jsonify(_load_pricing())


@bp.route("/api/usage/pricing", methods=["PUT"])
def set_pricing():
    """Replace the whole pricing map. Body: {model: {input_per_1m, output_per_1m, self_hosted}}."""
    body = request.get_json()
    if not isinstance(body, dict):
        return jsonify({"error": "Expected a JSON object of {model: {input_per_1m, output_per_1m, self_hosted}}"}), 400
    for model, entry in body.items():
        if not isinstance(entry, dict):
            return jsonify({"error": f"Pricing entry for '{model}' must be an object"}), 400
        for k in ("input_per_1m", "output_per_1m"):
            if k in entry and entry[k] is not None:
                try:
                    float(entry[k])
                except (TypeError, ValueError):
                    return jsonify({"error": f"'{model}'.{k} must be a number"}), 400
        if "self_hosted" in entry and not isinstance(entry["self_hosted"], bool):
            return jsonify({"error": f"'{model}'.self_hosted must be a boolean"}), 400
    Setting.set("llm.model_pricing", json.dumps(body), value_type="string")
    return jsonify({"success": True, "pricing": body})
