"""
Orion's Belt — Agent Execution Service
Autonomous tool loop with:
  - plan-before-execute gate (XML <plan> block with risk_level)
  - per-agent daily/monthly token budgets
  - step idempotency checkpointing (SHA-256)
  - remediation loop detection (same tool 3x in a row → fail)
  - post-completion reviewer agent
  - role-aware tool scoping
  - partial plan approval (per-step block/allow)

Usage:
    from app.services.agents import run_agent, approve_step, approve_plan, cancel_run

    agent_run = run_agent(agent_id="...", task_id="...")
    approve_plan(run_id="...", blocked_steps=[])
    approve_step(step_id="...", approved=True)
    cancel_run(run_id="...")
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime, timezone

log = logging.getLogger("orions-belt.agents")

TIER_WARN = 2
TIER_HARD_STOP = 3

_ROLE_KEYWORDS = {
    "deployment": ["deploy", "release", "rollout", "provision", "infra"],
    "investigation": ["log", "search", "query", "fetch", "read", "inspect", "debug"],
    "knowledge": ["note", "wiki", "doc", "knowledge", "write", "summarize"],
    "coordination": ["task", "plan", "assign", "notify", "message", "schedule"],
}

_ROLE_TOOL_SETS = {
    "deployment": {"shell", "run_command", "write_file", "deploy", "provision"},
    "investigation": {"read_file", "search_files", "query_db", "fetch_url", "list_directory"},
    "knowledge": {"read_file", "write_file", "search_files", "create_note"},
    "coordination": {"create_task", "update_task", "send_message", "schedule"},
}


def _now():
    return datetime.now(timezone.utc)


def _today() -> str:
    return date.today().isoformat()


def _this_month() -> str:
    return date.today().strftime("%Y-%m")


# ── Token budget helpers ──────────────────────────────────────────────────────

def _check_token_budget(agent, pending_tokens: int = 0) -> str | None:
    """Return error string if agent has exceeded its budget, else None.

    pending_tokens counts the current run's in-flight usage (not yet recorded in
    the ledger) so the budget can be enforced mid-run, not only at start.
    """
    from app import db
    from app.models.agent import TokenUsage
    from sqlalchemy import func

    if agent.daily_token_budget:
        used_today = db.session.query(func.sum(TokenUsage.tokens_used)).filter(
            TokenUsage.agent_id == agent.id,
            TokenUsage.period_day == _today(),
        ).scalar() or 0
        if used_today + pending_tokens >= agent.daily_token_budget:
            return (
                f"Daily token budget exceeded: {used_today + pending_tokens}/{agent.daily_token_budget} "
                f"tokens used on {_today()}"
            )

    if agent.monthly_token_budget:
        used_month = db.session.query(func.sum(TokenUsage.tokens_used)).filter(
            TokenUsage.agent_id == agent.id,
            TokenUsage.period_month == _this_month(),
        ).scalar() or 0
        if used_month + pending_tokens >= agent.monthly_token_budget:
            return (
                f"Monthly token budget exceeded: {used_month + pending_tokens}/{agent.monthly_token_budget} "
                f"tokens used in {_this_month()}"
            )

    return None


def _record_token_usage(agent_id: str, run_id: str | None, tokens: int) -> None:
    from app import db
    from app.models.agent import TokenUsage

    db.session.add(TokenUsage(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        run_id=run_id,
        tokens_used=tokens,
        period_day=_today(),
        period_month=_this_month(),
    ))
    db.session.commit()


# ── Plan extraction ───────────────────────────────────────────────────────────

def _extract_plan(text: str) -> dict | None:
    """Parse <plan>…</plan> from LLM response.

    Returns dict with risk_level, verify_steps, rollback_steps, raw_xml.
    Returns None if no plan block present.
    """
    m = re.search(r"<plan>(.*?)</plan>", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    raw = m.group(0)
    inner = m.group(1)

    risk_m = re.search(r"<risk_level>\s*(.*?)\s*</risk_level>", inner, re.IGNORECASE)
    risk_level = risk_m.group(1).lower().strip() if risk_m else "low"

    verify_steps = re.findall(r"<verify_step>(.*?)</verify_step>", inner, re.IGNORECASE)
    rollback_steps = re.findall(r"<rollback_step>(.*?)</rollback_step>", inner, re.IGNORECASE)

    return {
        "risk_level": risk_level,
        "verify_steps": verify_steps,
        "rollback_steps": rollback_steps,
        "raw_xml": raw,
    }


# ── Checkpointing ─────────────────────────────────────────────────────────────

def _compute_checkpoint_hash(tool_name: str, tool_args: dict) -> str:
    payload = json.dumps({"n": tool_name, "a": tool_args}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _find_checkpointed_step(run_id: str, checkpoint_hash: str):
    from app.models.agent import AgentStep

    return AgentStep.query.filter_by(
        run_id=run_id,
        checkpoint_hash=checkpoint_hash,
        is_checkpointed=True,
    ).first()


def _find_approved_pending_step(run_id: str, checkpoint_hash: str):
    """A step the operator already approved but that hasn't executed yet.

    On resume-after-approval the run loop re-reaches the same Tier-3 call; we
    must EXECUTE the approved step rather than pausing again (which would loop
    forever).
    """
    from app.models.agent import AgentStep

    return AgentStep.query.filter_by(
        run_id=run_id,
        checkpoint_hash=checkpoint_hash,
        required_approval=True,
        approved=True,
        is_checkpointed=False,
    ).first()


# ── Remediation loop detection ────────────────────────────────────────────────

def _is_remediation_loop(run_id: str, tool_name: str, tool_args: dict | None = None) -> bool:
    """Return True if the last 3 completed steps repeated the SAME tool AND args.

    Requiring matching args avoids failing legitimate serial use of one tool
    (e.g. reading three different files with read_file).
    """
    from app.models.agent import AgentStep

    recent = (
        AgentStep.query
        .filter_by(run_id=run_id)
        .filter(AgentStep.tool_output.isnot(None))
        .order_by(AgentStep.step_number.desc())
        .limit(3)
        .all()
    )
    if len(recent) < 3:
        return False

    target = json.dumps(tool_args or {}, sort_keys=True)

    def _norm(raw):
        try:
            return json.dumps(json.loads(raw or "{}"), sort_keys=True)
        except (ValueError, TypeError):
            return raw or "{}"

    return all(s.tool_name == tool_name and _norm(s.tool_input) == target for s in recent)


# ── Role-based tool scoping ───────────────────────────────────────────────────

def _infer_role(task_title: str, task_description: str) -> str | None:
    text = f"{task_title} {task_description or ''}".lower()
    for role, keywords in _ROLE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return role
    return None


def _filter_tools_by_role(tools: list, role: str | None) -> list:
    if not role or role == "auto":
        return tools
    allowed = _ROLE_TOOL_SETS.get(role)
    if not allowed:
        return tools
    filtered = [t for t in tools if any(a in t.name.lower() for a in allowed)]
    return filtered or tools  # fall back to all tools if filter would yield empty


# ── Reviewer agent ────────────────────────────────────────────────────────────

def _run_reviewer(run, result_summary: str, base_url: str, api_key: str, model: str) -> None:
    """One-shot reviewer LLM call to verdict the completed run."""
    from app import db
    from app.models.work import Task
    from app.services.llm import retry_with_recovery

    task = Task.query.get(run.task_id)
    if not task or not task.acceptance_criteria:
        run.reviewer_verdict = "skipped"
        db.session.commit()
        return

    review_prompt = (
        f"Task acceptance criteria:\n{task.acceptance_criteria}\n\n"
        f"Agent result summary:\n{result_summary}\n\n"
        "Does the result satisfy the acceptance criteria? "
        "Reply with exactly one word: APPROVED or REJECTED."
    )
    try:
        resp_text, _, _ = retry_with_recovery(
            base_url, api_key, model,
            [
                {"role": "system", "content": "You are a strict acceptance criteria reviewer."},
                {"role": "user", "content": review_prompt},
            ],
            [],
            max_retries=2,
        )
        verdict = "approved" if "APPROVED" in (resp_text or "").upper() else "rejected"
    except Exception as e:
        log.warning("reviewer.failed run=%s: %s", run.id, e)
        verdict = "skipped"

    run.reviewer_verdict = verdict
    if verdict == "rejected":
        run.status = "failed"
        run.error_message = "Reviewer rejected the result"
    db.session.commit()


# ── Public entry points ───────────────────────────────────────────────────────

def run_agent(agent_id: str, task_id: str, session_id: str | None = None) -> "AgentRun":
    """Execute an agent against a task. Returns when complete, failed, or paused."""
    from app import db
    from app.models.agent import Agent, AgentRun
    from app.models.work import Task

    agent = Agent.query.get(agent_id)
    if not agent:
        raise ValueError(f"Agent {agent_id} not found")

    task = Task.query.get(task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    budget_err = _check_token_budget(agent)
    if budget_err:
        log.warning("agent.budget_exceeded agent=%s: %s", agent_id, budget_err)
        run = AgentRun(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            task_id=task_id,
            status="failed",
            started_at=_now(),
            completed_at=_now(),
            error_message=budget_err,
        )
        db.session.add(run)
        db.session.commit()
        return run

    run = AgentRun(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        task_id=task_id,
        status="running",
        started_at=_now(),
        created_at=_now(),
    )
    db.session.add(run)
    agent.status = "running"
    db.session.commit()

    try:
        _execute_run(run, agent, task, session_id=session_id)
    except Exception as e:
        run.status = "failed"
        run.error_message = str(e)
        run.completed_at = _now()
        agent.status = "error"
        db.session.commit()
        log.error("agent.run_failed run=%s: %s", run.id, e)

    if run.tokens_used:
        _record_token_usage(agent_id, run.id, run.tokens_used)

    if run.status not in ("awaiting_approval", "pending_validation"):
        agent.status = "idle"
        db.session.commit()

    return run


def approve_plan(run_id: str, blocked_steps: list[str] | None = None) -> bool:
    """Approve a pending_validation run, optionally blocking specific steps by name."""
    from app import db
    from app.models.agent import Agent, AgentRun
    from app.models.work import Task

    run = AgentRun.query.get(run_id)
    if not run or run.status != "pending_validation":
        return False

    run.plan_approved = True
    run.blocked_steps_json = json.dumps(blocked_steps or [])
    run.status = "running"
    db.session.commit()

    agent = Agent.query.get(run.agent_id)
    task = Task.query.get(run.task_id)
    if agent and task:
        try:
            _execute_run(run, agent, task)
        except Exception as e:
            run.status = "failed"
            run.error_message = str(e)
            run.completed_at = _now()
            db.session.commit()
            log.error("agent.resume_failed run=%s: %s", run.id, e)
        finally:
            if run.tokens_used:
                _record_token_usage(run.agent_id, run.id, run.tokens_used)
            if agent.status == "running":
                agent.status = "idle"
                db.session.commit()

    return True


def approve_step(step_id: str, approved: bool = True) -> bool:
    """Approve or reject a pending Tier 3 agent step."""
    from app import db
    from app.models.agent import Agent, AgentStep, AgentRun

    step = AgentStep.query.get(step_id)
    if not step:
        return False

    step.approved = approved
    step.approved_at = _now()

    run = AgentRun.query.get(step.run_id)
    if run:
        if approved:
            run.status = "running"
            db.session.commit()
            # Resume execution so the approved tool actually runs
            agent = Agent.query.get(run.agent_id)
            from app.models.work import Task
            task = Task.query.get(run.task_id)
            if agent and task:
                try:
                    _execute_run(run, agent, task)
                except Exception as e:
                    run.status = "failed"
                    run.error_message = str(e)
                    run.completed_at = _now()
                    db.session.commit()
                    log.error("agent.resume_after_approval run=%s: %s", run.id, e)
                finally:
                    if run.tokens_used:
                        _record_token_usage(run.agent_id, run.id, run.tokens_used)
                    if agent.status == "running":
                        agent.status = "idle"
                        db.session.commit()
        else:
            run.status = "cancelled"
            run.completed_at = _now()
            run.error_message = "Step rejected by user"
            db.session.commit()

    return True


def cancel_run(run_id: str) -> bool:
    """Cancel a running or paused agent run."""
    from app import db
    from app.models.agent import AgentRun, Agent

    run = AgentRun.query.get(run_id)
    if not run:
        return False

    run.status = "cancelled"
    run.completed_at = _now()

    agent = Agent.query.get(run.agent_id)
    if agent:
        agent.status = "idle"

    db.session.commit()
    return True


# ── Inner execution loop ──────────────────────────────────────────────────────

def _execute_run(run, agent, task, session_id: str | None = None):
    """LLM → tool loop: plan gate → checkpointing → loop detection → reviewer."""
    import time as _time
    from app import db
    from app.models.agent import AgentStep
    from app.models.logs import AgentTrace
    from app.models.mcp_tool import MCPTool
    from app.models.settings import Setting
    from app.services.llm import build_tool_definitions, inject_knowledge_context, retry_with_recovery
    from app.services.mcp.tools import execute_tool
    from config import Config

    llm_providers_raw = Setting.get("llm.providers")
    llm_active_id = Setting.get("llm.active_provider")
    llm_providers = (
        json.loads(llm_providers_raw) if isinstance(llm_providers_raw, str)
        else (llm_providers_raw or [])
    )
    active_provider = None
    if llm_active_id:
        active_provider = next((p for p in llm_providers if p.get("id") == llm_active_id), None)
    if not active_provider and llm_providers:
        active_provider = llm_providers[0]

    base_url = (active_provider or {}).get("base_url", Config.LLM_BASE_URL)
    raw_key = (active_provider or {}).get("api_key", Config.LLM_API_KEY)
    _plain_prefixes = ("sk-", "sk-proj-", "ghp_", "glpat-", "xoxb-", "xoxp-", "AIza", "EA")
    if raw_key and not any(raw_key.startswith(p) for p in _plain_prefixes):
        try:
            from app.services.crypto import decrypt_data
            raw_key = decrypt_data(raw_key) or raw_key
        except Exception:
            pass
    api_key = raw_key
    model = agent.llm_model_override or (active_provider or {}).get("model", Config.LLM_MODEL)

    allowed_tools = json.loads(agent.allowed_tools or "[]")
    if allowed_tools:
        tools_q = MCPTool.query.filter(MCPTool.name.in_(allowed_tools), MCPTool.enabled == True)
    else:
        tools_q = MCPTool.query.filter_by(enabled=True)
    tools = tools_q.all()

    effective_role = agent.role_scope or _infer_role(task.title, task.description or "")
    tools = _filter_tools_by_role(tools, effective_role)
    tool_defs = build_tool_definitions(tools)
    tool_tier_map = {t.name: t.tier for t in tools}

    blocked_steps: list[str] = json.loads(run.blocked_steps_json or "[]")

    system_prompt = agent.system_prompt or (
        f"You are {agent.name}, an AI agent. Complete the assigned task using the available tools.\n\n"
        "Before high-risk actions output a <plan> block:\n"
        "<plan><risk_level>low|medium|high|critical</risk_level>"
        "<verify_step>...</verify_step><rollback_step>...</rollback_step></plan>"
    )
    task_context = (
        f"Task: {task.title}\n"
        f"Description: {task.description or 'No description provided'}\n"
    )
    if task.acceptance_criteria:
        task_context += f"Acceptance criteria:\n{task.acceptance_criteria}\n"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Please complete the following task:\n\n{task_context}"},
    ]
    messages = inject_knowledge_context(messages, f"{task.title} {task.description or ''}")

    # Honor the configured value up to the same hard ceiling the API enforces
    # (routes cap max_iterations at 50); previously this silently clamped to 20.
    max_iter = min(agent.max_iterations or 20, 50)
    total_tokens = 0
    plan_checked = run.plan_approved is True  # True if resuming after approval

    for iteration in range(max_iter):
        run.iterations_used = iteration + 1
        _step_start = _time.time()

        response_text, tool_calls, tokens_used = retry_with_recovery(
            base_url, api_key, model, messages, tool_defs
        )
        total_tokens += tokens_used
        run.tokens_used = total_tokens

        # Enforce the budget mid-run, including this run's in-flight tokens, so a
        # single long run (or a resumed one) can't blow past the limit.
        budget_err = _check_token_budget(agent, pending_tokens=total_tokens)
        if budget_err:
            run.status = "failed"
            run.error_message = budget_err
            run.completed_at = _now()
            db.session.commit()
            log.warning("agent.budget_exceeded_midrun run=%s: %s", run.id, budget_err)
            return

        if response_text:
            messages.append({"role": "assistant", "content": response_text})

        # Plan gate — only on first iteration, only when not already approved
        if not plan_checked and iteration == 0 and response_text:
            plan = _extract_plan(response_text)
            if plan and plan["risk_level"] in ("high", "critical"):
                run.plan_xml = plan["raw_xml"]
                run.plan_approved = None
                run.status = "pending_validation"
                db.session.commit()
                log.info("agent.plan_gate run=%s risk=%s — paused", run.id, plan["risk_level"])
                return
            plan_checked = True

        if not tool_calls:
            run.result_summary = response_text or "Task completed."
            run.status = "completed"
            run.completed_at = _now()
            trace = AgentTrace(
                run_id=run.id, step=iteration * 10,
                trace_type="completed", content=(response_text or "")[:2000],
                model_used=model,
            )
            db.session.add(trace)
            db.session.commit()
            _run_reviewer(run, run.result_summary, base_url, api_key, model)
            return

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args", {})
            tool_id = tc.get("id", str(uuid.uuid4()))
            tier = tool_tier_map.get(tool_name, 0)

            if _is_remediation_loop(run.id, tool_name, tool_args):
                run.status = "failed"
                run.error_message = f"Remediation loop: {tool_name} called 3× with identical args"
                run.completed_at = _now()
                run.remediation_attempts = (run.remediation_attempts or 0) + 1
                db.session.commit()
                log.warning("agent.remediation_loop run=%s tool=%s", run.id, tool_name)
                return

            if tool_name in blocked_steps:
                step = AgentStep(
                    id=str(uuid.uuid4()),
                    run_id=run.id,
                    step_number=(iteration * 10) + tool_calls.index(tc),
                    tool_name=tool_name,
                    tool_input=json.dumps(tool_args),
                    blocked=True,
                    created_at=_now(),
                )
                db.session.add(step)
                db.session.commit()
                # A tool result must follow an assistant tool_calls message or
                # the provider rejects it (orphan tool message → 400).
                messages.append({
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": tool_id, "type": "function",
                                    "function": {"name": tool_name, "arguments": json.dumps(tool_args)}}],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"[BLOCKED] '{tool_name}' was blocked by operator.",
                })
                continue

            chk_hash = _compute_checkpoint_hash(tool_name, tool_args)
            existing = _find_checkpointed_step(run.id, chk_hash)
            if existing:
                messages.append({
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": tool_id, "type": "function",
                                    "function": {"name": tool_name, "arguments": json.dumps(tool_args)}}],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": existing.tool_output or "[checkpointed]",
                })
                continue

            # Resume-after-approval: if the operator already approved this exact
            # call, execute the existing step now instead of creating a new one
            # and pausing again (which looped forever).
            step = _find_approved_pending_step(run.id, chk_hash)
            if step is None:
                step = AgentStep(
                    id=str(uuid.uuid4()),
                    run_id=run.id,
                    step_number=(iteration * 10) + tool_calls.index(tc),
                    tool_name=tool_name,
                    tool_input=json.dumps(tool_args),
                    required_approval=(tier >= TIER_HARD_STOP),
                    checkpoint_hash=chk_hash,
                    created_at=_now(),
                )
                db.session.add(step)
                db.session.commit()

                if tier >= TIER_HARD_STOP:
                    run.status = "awaiting_approval"
                    db.session.commit()
                    log.info("agent.tier3_pause run=%s step=%s", run.id, step.id)
                    return

            try:
                import asyncio
                try:
                    result = asyncio.get_event_loop().run_until_complete(
                        execute_tool(tool_name, tool_args, session_id=session_id, run_id=str(run.id))
                    )
                except RuntimeError:
                    result = asyncio.run(
                        execute_tool(tool_name, tool_args, session_id=session_id, run_id=str(run.id))
                    )
            except Exception as e:
                result = f"Error executing {tool_name}: {e}"

            step.tool_output = str(result)[:4096]
            step.approved = True
            step.approved_at = _now()
            step.is_checkpointed = True
            elapsed_ms = int((_time.time() - _step_start) * 1000)

            trace = AgentTrace(
                run_id=run.id,
                step=(iteration * 10) + tool_calls.index(tc),
                trace_type="tool_call",
                tool_name=tool_name,
                tool_args=json.dumps(tool_args)[:500],
                tool_result=str(result)[:1000],
                model_used=model,
                duration_ms=elapsed_ms,
            )
            db.session.add(trace)
            db.session.commit()
            _step_start = _time.time()

            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": tool_id, "type": "function",
                                "function": {"name": tool_name, "arguments": json.dumps(tool_args)}}],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": str(result),
            })

    run.status = "failed"
    run.error_message = f"Exceeded max iterations ({max_iter})"
    run.completed_at = _now()
    db.session.commit()


# ── Re-exports ────────────────────────────────────────────────────────────────
from app.services.llm import RecoveryError, TransientError, RoleOrderError, ContextTooLargeError  # noqa: E402, F401
