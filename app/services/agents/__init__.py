"""
Orion's Belt — Agent Execution Service
Autonomous tool loop with tier-based approval flow.

An Agent iterates up to max_iterations times:
  1. Call LLM with system prompt + task context + tool definitions
  2. If tool_calls in response: execute tool, append result, continue
  3. If Tier 2/3 tool: pause run with status "awaiting_approval"
  4. If no tool_calls: store final answer, mark completed

Usage:
    from app.services.agents import run_agent, approve_step, cancel_run

    # Start a run (synchronous, blocks until complete or awaiting_approval)
    agent_run = run_agent(agent_id=1, task_id="task-uuid")

    # Approve a pending step
    approve_step(step_id="step-uuid", approved=True)

    # Cancel a run
    cancel_run(run_id="run-uuid")
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx

log = logging.getLogger("orions-belt.agents")

TIER_WARN = 2     # requires countdown confirmation
TIER_HARD_STOP = 3  # requires explicit human approval


def _now():
    return datetime.now(timezone.utc)


def run_agent(agent_id: str, task_id: str) -> "AgentRun":
    """Execute an agent against a task.

    Creates an AgentRun record and runs the tool loop synchronously.
    Returns when the run completes, fails, or reaches awaiting_approval.

    Args:
        agent_id: ID of the Agent to run
        task_id: ID of the Task to work on

    Returns:
        The AgentRun ORM instance (check .status for outcome)
    """
    from app import db
    from app.models.agent import Agent, AgentRun, AgentStep
    from app.models.work import Task
    from app.models.mcp_tool import MCPTool
    from app.models.settings import Setting
    from app.services.llm import build_tool_definitions
    from app.services.mcp.tools import execute_tool, TIER_HARD_STOP as MCP_HARD_STOP
    from config import Config

    agent = Agent.query.get(agent_id)
    if not agent:
        raise ValueError(f"Agent {agent_id} not found")

    task = Task.query.get(task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    # Create run record
    run = AgentRun(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        task_id=task_id,
        status="running",
        started_at=_now(),
        created_at=_now(),
    )
    db.session.add(run)

    # Update agent status
    agent.status = "running"
    db.session.commit()

    try:
        _execute_run(run, agent, task)
    except Exception as e:
        run.status = "failed"
        run.error_message = str(e)
        run.completed_at = _now()
        agent.status = "error"
        db.session.commit()
        log.error(f"Agent run {run.id} failed: {e}")

    if run.status not in ("awaiting_approval",):
        agent.status = "idle"
        db.session.commit()

    return run


def _execute_run(run, agent, task):
    """Inner loop: LLM call → tool execution → repeat."""
    from app import db
    from app.models.agent import AgentStep
    from app.models.mcp_tool import MCPTool
    from app.models.settings import Setting
    from app.services.llm import build_tool_definitions
    from app.services.mcp.tools import execute_tool
    from config import Config

    # Build system prompt
    system_prompt = agent.system_prompt or (
        f"You are {agent.name}, an AI agent. Complete the assigned task using the available tools."
    )

    # Task context
    task_context = (
        f"Task: {task.title}\n"
        f"Description: {task.description or 'No description provided'}\n"
    )
    if task.acceptance_criteria:
        task_context += f"Acceptance criteria:\n{task.acceptance_criteria}\n"

    # LLM config — use agent override or fall back to global
    llm_providers_raw = Setting.get("llm.providers")
    llm_active_id = Setting.get("llm.active_provider")

    if isinstance(llm_providers_raw, str):
        try:
            llm_providers = json.loads(llm_providers_raw)
        except Exception:
            llm_providers = []
    else:
        llm_providers = llm_providers_raw or []

    active_provider = None
    if llm_active_id:
        active_provider = next((p for p in llm_providers if p.get("id") == llm_active_id), None)
    if not active_provider and llm_providers:
        active_provider = llm_providers[0]

    base_url = (active_provider or {}).get("base_url", Config.LLM_BASE_URL)
    api_key = (active_provider or {}).get("api_key", Config.LLM_API_KEY)
    model = agent.llm_model_override or (active_provider or {}).get("model", Config.LLM_MODEL)

    # Determine available tools
    allowed_tools = json.loads(agent.allowed_tools or "[]")
    if allowed_tools:
        tools_q = MCPTool.query.filter(
            MCPTool.name.in_(allowed_tools), MCPTool.enabled == True
        )
    else:
        tools_q = MCPTool.query.filter_by(enabled=True)
    tools = tools_q.all()
    tool_defs = build_tool_definitions(tools)
    tool_tier_map = {t.name: t.tier for t in tools}

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Please complete the following task:\n\n{task_context}"},
    ]

    max_iter = min(agent.max_iterations, 20)  # hard cap
    total_tokens = 0

    for iteration in range(max_iter):
        run.iterations_used = iteration + 1

        # LLM call
        response_text, tool_calls, tokens_used = _call_llm(
            base_url, api_key, model, messages, tool_defs
        )
        total_tokens += tokens_used
        run.tokens_used = total_tokens

        if response_text:
            messages.append({"role": "assistant", "content": response_text})

        if not tool_calls:
            # Agent is done
            run.result_summary = response_text or "Task completed."
            run.status = "completed"
            run.completed_at = _now()
            db.session.commit()
            return

        # Process each tool call
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args", {})
            tool_id = tc.get("id", str(uuid.uuid4()))
            tier = tool_tier_map.get(tool_name, 0)

            step = AgentStep(
                id=str(uuid.uuid4()),
                run_id=run.id,
                step_number=(iteration * 10) + tool_calls.index(tc),
                tool_name=tool_name,
                tool_input=json.dumps(tool_args),
                required_approval=(tier >= TIER_HARD_STOP),
                created_at=_now(),
            )
            db.session.add(step)
            db.session.commit()

            # Tier 3: pause for human approval
            if tier >= TIER_HARD_STOP:
                run.status = "awaiting_approval"
                db.session.commit()
                log.info(f"Agent run {run.id} paused at step {step.id} — Tier 3 approval required")
                return

            # Execute tool
            try:
                import asyncio
                result = asyncio.get_event_loop().run_until_complete(
                    execute_tool(tool_name, tool_args)
                )
            except RuntimeError:
                # No event loop running — create one
                import asyncio
                result = asyncio.run(execute_tool(tool_name, tool_args))
            except Exception as e:
                result = f"Error executing {tool_name}: {e}"

            step.tool_output = result
            step.approved = True
            step.approved_at = _now()
            db.session.commit()

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

    # Reached max iterations
    run.status = "failed"
    run.error_message = f"Exceeded max iterations ({max_iter})"
    run.completed_at = _now()
    db.session.commit()


def _call_llm(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    tool_defs: list,
) -> tuple[str, list, int]:
    """Make a synchronous LLM call and return (response_text, tool_calls, tokens).

    Returns:
        response_text: The assistant's text response (may be empty if tool calls)
        tool_calls: List of {"id": ..., "name": ..., "args": ...} dicts
        tokens: Total tokens used
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tool_defs:
        body["tools"] = tool_defs

    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"LLM API error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}")

    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    response_text = msg.get("content") or ""

    raw_tool_calls = msg.get("tool_calls", [])
    tool_calls = []
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({
            "id": tc.get("id", str(uuid.uuid4())),
            "name": fn.get("name", ""),
            "args": args,
        })

    usage = data.get("usage", {})
    tokens = usage.get("total_tokens", 0)

    return response_text, tool_calls, tokens


def approve_step(step_id: str, approved: bool = True) -> bool:
    """Approve or reject a pending Tier 3 agent step.

    If approved, the step is marked and the run can be resumed.
    If rejected, the run is cancelled.

    Returns True if the step was found and updated, False otherwise.
    """
    from app import db
    from app.models.agent import AgentStep, AgentRun

    step = AgentStep.query.get(step_id)
    if not step:
        return False

    step.approved = approved
    step.approved_at = _now()

    run = AgentRun.query.get(step.run_id)
    if run:
        if approved:
            run.status = "running"
        else:
            run.status = "cancelled"
            run.completed_at = _now()
            run.error_message = "Step rejected by user"

    db.session.commit()
    return True


def cancel_run(run_id: str) -> bool:
    """Cancel a running or paused agent run.

    Returns True if found and cancelled, False if not found.
    """
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
