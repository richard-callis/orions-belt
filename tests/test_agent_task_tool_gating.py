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


class TestExecuteRunToolAccessMatchesAgentRuntime:
    """Regression test: _execute_run used to additionally narrow an agent's
    tools by a role INFERRED FROM THE TASK'S OWN TITLE/DESCRIPTION
    (_infer_role + _filter_tools_by_role, now removed) — so the exact same
    agent, with the exact same allowed_tools, could lose access to tools
    depending purely on how a task happened to be worded, and got a
    different tool set in a Task run than it would in a chat room (which
    never applied this filtering at all). Tool access must be a function of
    the agent alone, everywhere it runs — see AgentRuntime.tools()."""

    def test_task_wording_does_not_narrow_agents_allowed_tools(self, app, monkeypatch):
        import app.services.llm as llm_mod
        import app.services.mcp.tools as mcp_tools_mod

        captured_tool_names = []

        def fake_build_tool_definitions(tools):
            captured_tool_names.extend(t.name for t in tools)
            return []

        def fake_retry(*a, **k):
            return "done, no tools needed", [], 1

        async def fake_execute_tool(name, args, **kwargs):
            return "unused"

        monkeypatch.setattr(llm_mod, "retry_with_recovery", fake_retry)
        monkeypatch.setattr(llm_mod, "build_tool_definitions", fake_build_tool_definitions)
        monkeypatch.setattr(mcp_tools_mod, "execute_tool", fake_execute_tool)

        with app.app_context():
            # create_word_document isn't in any of the old _ROLE_TOOL_SETS
            # buckets, and the task below is worded to trigger the old
            # "investigation" role (keywords: log, search, inspect) — under
            # the removed filtering, create_word_document would have been
            # silently dropped even though the agent is explicitly allowed
            # to use it.
            #
            # get-or-create rather than a bare add(): MCPTool.name is
            # unique, and both of these names are commonly seeded elsewhere
            # (e.g. _seed_builtin_tools-style fixtures) into the shared
            # session-scoped test DB — adding unconditionally would collide
            # depending on test collection/run order.
            created_tool_ids = []
            for name in ("read_file", "create_word_document"):
                if not MCPTool.query.filter_by(name=name).first():
                    tool_id = str(uuid.uuid4())
                    db.session.add(MCPTool(id=tool_id, name=name, tier=0, enabled=True, source="builtin"))
                    created_tool_ids.append(tool_id)

            agent = Agent(id=str(uuid.uuid4()), name="a",
                          allowed_tools='["read_file", "create_word_document"]',
                          status="idle", max_iterations=1)
            db.session.add(agent)
            db.session.commit()

            p = Project(id=str(uuid.uuid4()), name="P")
            e = Epic(id=str(uuid.uuid4()), project_id=p.id, title="E")
            f = Feature(id=str(uuid.uuid4()), epic_id=e.id, title="F")
            task = Task(id=str(uuid.uuid4()), feature_id=f.id,
                       title="Search and inspect the logs",
                       description="Debug the issue by reading log output")
            db.session.add_all([p, e, f, task])
            db.session.commit()
            try:
                run_agent(agent.id, task.id)
                # Genuinely checks parity with AgentRuntime, not just a
                # hardcoded expectation that happens to match today.
                from app.services.agents.runtime import AgentRuntime
                expected = {t.name for t in AgentRuntime(agent).tools()}
                assert set(captured_tool_names) == expected == {"read_file", "create_word_document"}
            finally:
                AgentRun.query.filter_by(agent_id=agent.id).delete()
                for tool_id in created_tool_ids:
                    MCPTool.query.filter_by(id=tool_id).delete()
                Task.query.filter_by(id=task.id).delete()
                Feature.query.filter_by(id=f.id).delete()
                Epic.query.filter_by(id=e.id).delete()
                Project.query.filter_by(id=p.id).delete()
                db.session.delete(agent)
                db.session.commit()
