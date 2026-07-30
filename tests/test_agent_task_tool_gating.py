"""
Regression test for a tool-tier-ceiling bypass in the autonomous task-runner
loop (_execute_run in app/services/agents/__init__.py) — the same class of
bug fixed in AgentRuntime.chat_reply (tests/test_agent_runtime.py's
TestUnknownToolNameRefused). tool_tier_map.get(tool_name, 0) used to default
an unrecognized tool name to tier 0, which skips BOTH the agent's
allowed_tools filter and the `tier >= TIER_HARD_STOP` approval pause — so a
hallucinated/disallowed high-tier tool name would execute immediately with
no approval step at all, worse than the chat_reply case since this path
auto-executes tier 0-2 without even a ceiling check.
"""
import uuid

from app import db
from app.models.agent import Agent, AgentRun
from app.models.mcp_tool import MCPTool
from app.models.work import Epic, Feature, Project, Task
from app.services.agents import run_agent


def _make_task(app):
    p = Project(id=str(uuid.uuid4()), name="P")
    e = Epic(id=str(uuid.uuid4()), project_id=p.id, title="E")
    f = Feature(id=str(uuid.uuid4()), epic_id=e.id, title="F")
    t = Task(id=str(uuid.uuid4()), feature_id=f.id, title="T", description="d")
    db.session.add_all([p, e, f, t])
    db.session.commit()
    return t


class TestExecuteRunUnknownToolNameRefused:
    def test_hallucinated_tool_name_is_refused_not_auto_executed(self, app, monkeypatch):
        import app.services.llm as llm_mod
        import app.services.mcp.tools as mcp_tools_mod

        executed = {"n": 0}

        async def fake_execute_tool(name, args, **kwargs):
            executed["n"] += 1
            return "should never run"

        def fake_retry(*a, **k):
            # Always emits a call to a Tier-3 tool that isn't in this agent's
            # tier map at all (not seeded/allowed) — simulating a
            # hallucinated or disallowed tool name.
            return "calling a tool", [{"id": "1", "name": "delete_file", "args": {"path": "/x"}}], 1

        monkeypatch.setattr(llm_mod, "retry_with_recovery", fake_retry)
        monkeypatch.setattr(llm_mod, "build_tool_definitions", lambda tools: [])
        monkeypatch.setattr(mcp_tools_mod, "execute_tool", fake_execute_tool)

        with app.app_context():
            agent = Agent(id=str(uuid.uuid4()), name="a", allowed_tools='["read_file"]',
                          status="idle", max_iterations=3)
            db.session.add(agent)
            db.session.add(MCPTool(id="t-gate1", name="read_file", tier=0, enabled=True, source="builtin"))
            db.session.commit()
            task = _make_task(app)
            try:
                run = run_agent(agent.id, task.id)
                # The real executor (and thus the Tier-3 tool) must never run.
                assert executed["n"] == 0
                # Never silently "completed" or paused for approval — it ran
                # out of iterations refusing the same unknown call each time,
                # which is the correct failure mode for a hallucinated tool.
                assert run.status == "failed"
                assert "Exceeded max iterations" in run.error_message
            finally:
                AgentRun.query.filter_by(agent_id=agent.id).delete()
                MCPTool.query.filter_by(id="t-gate1").delete()
                Task.query.filter_by(id=task.id).delete()
                Feature.query.filter_by(id=task.feature_id).delete()
                db.session.delete(agent)
                db.session.commit()
