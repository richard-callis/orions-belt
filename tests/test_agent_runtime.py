"""
Tests for AgentRuntime — the shared 'an agent runs its tools' capability.
"""
from types import SimpleNamespace

from app import db
from app.models.connector import AuthorizedDirectory
from app.models.mcp_tool import MCPTool
from app.services.agents.runtime import AgentRuntime


def _agent(allowed="[]"):
    return SimpleNamespace(id="a1", name="A1", allowed_tools=allowed, llm_model_override=None,
                           daily_token_budget=None, monthly_token_budget=None)


class TestAgentRuntimeTools:
    def test_refuses_high_tier_tool(self, app):
        with app.app_context():
            rt = AgentRuntime(_agent(), provider={"base_url": "x", "api_key": "y", "model": "m"})
            out = rt.run_tool("delete_file", {"path": "/x"}, tier=3)
            assert out.startswith("[Refused]")
            assert "Task" in out   # points the user at the approval path

    def test_allows_low_tier_by_default(self, app, monkeypatch):
        # A Tier-0 tool is within the default ceiling → it is dispatched (we stub
        # the actual executor so no real tool runs).
        import app.services.mcp.tools as mcp_tools
        monkeypatch.setattr(mcp_tools, "run_tool_sync", lambda name, args, **k: f"ran:{name}")
        with app.app_context():
            rt = AgentRuntime(_agent(), provider={"base_url": "x", "api_key": "y", "model": "m"})
            out = rt.run_tool("read_file", {"path": "/x"}, tier=0)
            assert out == "ran:read_file"

    def test_tools_respects_allowlist(self, app):
        with app.app_context():
            db.session.add_all([
                MCPTool(id="t1", name="read_file", tier=0, enabled=True, source="builtin"),
                MCPTool(id="t2", name="delete_file", tier=3, enabled=True, source="builtin"),
            ])
            db.session.commit()
            try:
                rt = AgentRuntime(_agent('["read_file"]'),
                                  provider={"base_url": "x", "api_key": "y", "model": "m"})
                names = {t.name for t in rt.tools()}
                assert names == {"read_file"}   # only the allowlisted tool
            finally:
                MCPTool.query.filter(MCPTool.id.in_(["t1", "t2"])).delete(synchronize_session=False)
                db.session.commit()


class TestAuthorizedDirsBlock:
    def test_empty_when_agent_has_no_file_tools(self, app):
        with app.app_context():
            db.session.add(MCPTool(id="t-search", name="search_emails", tier=0, enabled=True, source="builtin"))
            db.session.add(AuthorizedDirectory(path="/data/x", alias="X", enabled=True))
            db.session.commit()
            try:
                rt = AgentRuntime(_agent('["search_emails"]'),
                                  provider={"base_url": "x", "api_key": "y", "model": "m"})
                assert rt._authorized_dirs_block() == ""
            finally:
                MCPTool.query.filter_by(id="t-search").delete()
                AuthorizedDirectory.query.filter_by(path="/data/x").delete()
                db.session.commit()

    def test_empty_when_no_directories_configured(self, app):
        with app.app_context():
            db.session.add(MCPTool(id="t-read", name="read_file", tier=0, enabled=True, source="builtin"))
            db.session.commit()
            try:
                rt = AgentRuntime(_agent('["read_file"]'),
                                  provider={"base_url": "x", "api_key": "y", "model": "m"})
                assert rt._authorized_dirs_block() == ""
            finally:
                MCPTool.query.filter_by(id="t-read").delete()
                db.session.commit()

    def test_lists_authorized_directories_for_file_tool_agent(self, app):
        with app.app_context():
            db.session.add(MCPTool(id="t-read2", name="read_file", tier=0, enabled=True, source="builtin"))
            db.session.add(AuthorizedDirectory(path="/data/projects", alias="Projects", enabled=True))
            db.session.commit()
            try:
                rt = AgentRuntime(_agent('["read_file"]'),
                                  provider={"base_url": "x", "api_key": "y", "model": "m"})
                block = rt._authorized_dirs_block()
                assert "Projects: /data/projects" in block
                assert "Authorized directories" in block
            finally:
                MCPTool.query.filter_by(id="t-read2").delete()
                AuthorizedDirectory.query.filter_by(path="/data/projects").delete()
                db.session.commit()

    def test_disabled_directory_excluded(self, app):
        with app.app_context():
            db.session.add(MCPTool(id="t-read3", name="read_file", tier=0, enabled=True, source="builtin"))
            db.session.add(AuthorizedDirectory(path="/data/off", alias="Off", enabled=False))
            db.session.commit()
            try:
                rt = AgentRuntime(_agent('["read_file"]'),
                                  provider={"base_url": "x", "api_key": "y", "model": "m"})
                assert rt._authorized_dirs_block() == ""
            finally:
                MCPTool.query.filter_by(id="t-read3").delete()
                AuthorizedDirectory.query.filter_by(path="/data/off").delete()
                db.session.commit()

    def test_chat_reply_prepends_dirs_block_to_system_message(self, app, monkeypatch):
        import app.services.llm as llm_mod

        captured = {}

        def fake_retry(base_url, api_key, model, convo, tool_defs, max_retries=2, session_id=None, run_id=None):
            captured["system_content"] = convo[0]["content"]
            return "ok", [], 0

        with app.app_context():
            db.session.add(MCPTool(id="t-read4", name="read_file", tier=0, enabled=True, source="builtin"))
            db.session.add(AuthorizedDirectory(path="/data/y", alias="Y", enabled=True))
            db.session.commit()
            try:
                monkeypatch.setattr(llm_mod, "build_tool_definitions", lambda tools: [])
                monkeypatch.setattr(llm_mod, "retry_with_recovery", fake_retry)
                rt = AgentRuntime(_agent('["read_file"]'),
                                  provider={"base_url": "x", "api_key": "y", "model": "m"})
                rt.chat_reply([{"role": "system", "content": "You are an agent."}])
                assert "You are an agent." in captured["system_content"]
                assert "Y: /data/y" in captured["system_content"]
            finally:
                MCPTool.query.filter_by(id="t-read4").delete()
                AuthorizedDirectory.query.filter_by(path="/data/y").delete()
                db.session.commit()


def _real_agent(**overrides):
    from app.models.agent import Agent
    kwargs = dict(id="ra1", name="RealAgent", allowed_tools="[]", llm_model_override=None,
                 daily_token_budget=None, monthly_token_budget=None, max_iterations=20,
                 status="idle")
    kwargs.update(overrides)
    return Agent(**kwargs)


class TestChatReplyTokenBudget:
    def test_stops_immediately_if_already_over_daily_budget(self, app, monkeypatch):
        import app.services.llm as llm_mod
        from app.models.agent import TokenUsage
        from app.services.agents import _today, _this_month

        called = {"n": 0}
        def fake_retry(*a, **k):
            called["n"] += 1
            return "should not be reached", [], 100

        monkeypatch.setattr(llm_mod, "build_tool_definitions", lambda tools: [])
        monkeypatch.setattr(llm_mod, "retry_with_recovery", fake_retry)
        with app.app_context():
            agent = _real_agent(id="ra-budget1", daily_token_budget=100)
            db.session.add(agent)
            db.session.add(TokenUsage(id="tu1", agent_id="ra-budget1", tokens_used=100,
                                      period_day=_today(), period_month=_this_month()))
            db.session.commit()
            try:
                rt = AgentRuntime(agent, provider={"base_url": "x", "api_key": "y", "model": "m"})
                reply = rt.chat_reply([{"role": "system", "content": "hi"}])
                assert reply.startswith("[Stopped]")
                assert "budget" in reply.lower()
                assert called["n"] == 0   # never even called the LLM
            finally:
                TokenUsage.query.filter_by(agent_id="ra-budget1").delete()
                agent2 = db.session.get(type(agent), "ra-budget1")
                if agent2:
                    db.session.delete(agent2)
                db.session.commit()

    def test_records_token_usage_after_successful_reply(self, app, monkeypatch):
        import app.services.llm as llm_mod
        from app.models.agent import TokenUsage

        monkeypatch.setattr(llm_mod, "build_tool_definitions", lambda tools: [])
        monkeypatch.setattr(llm_mod, "retry_with_recovery",
                            lambda *a, **k: ("hello", [], 42))
        with app.app_context():
            agent = _real_agent(id="ra-budget2")
            db.session.add(agent)
            db.session.commit()
            try:
                rt = AgentRuntime(agent, provider={"base_url": "x", "api_key": "y", "model": "m"})
                reply = rt.chat_reply([{"role": "system", "content": "hi"}])
                assert reply == "hello"
                row = TokenUsage.query.filter_by(agent_id="ra-budget2").first()
                assert row is not None
                assert row.tokens_used == 42
                assert row.run_id is None   # never a dangling FK to a fake run_id
            finally:
                TokenUsage.query.filter_by(agent_id="ra-budget2").delete()
                agent2 = db.session.get(type(agent), "ra-budget2")
                if agent2:
                    db.session.delete(agent2)
                db.session.commit()


class TestChatReplyLoopDetection:
    def test_stops_after_repeated_identical_tool_call(self, app, monkeypatch):
        import app.services.llm as llm_mod

        # Always "calls" the same tool with the same args — a model stuck in a loop.
        def fake_retry(*a, **k):
            return "trying again", [{"id": "1", "name": "read_file", "args": {"path": "/x"}}], 1

        monkeypatch.setattr(llm_mod, "build_tool_definitions", lambda tools: [])
        monkeypatch.setattr(llm_mod, "retry_with_recovery", fake_retry)
        monkeypatch.setattr(AgentRuntime, "run_tool", lambda self, *a, **k: "some result")
        with app.app_context():
            agent = _real_agent(id="ra-loop1")
            db.session.add(agent)
            db.session.commit()
            try:
                rt = AgentRuntime(agent, provider={"base_url": "x", "api_key": "y", "model": "m"})
                tool_log = []
                rt.chat_reply([{"role": "system", "content": "hi"}],
                              max_tool_iters=10, tool_log=tool_log)
                # Stopped well before exhausting max_tool_iters=10.
                assert len(tool_log) < 10
                assert any(t["refused"] and "loop" in t["result"].lower() for t in tool_log)
            finally:
                from app.models.agent import TokenUsage
                TokenUsage.query.filter_by(agent_id="ra-loop1").delete()
                agent2 = db.session.get(type(agent), "ra-loop1")
                if agent2:
                    db.session.delete(agent2)
                db.session.commit()

    def test_different_args_not_treated_as_a_loop(self, app, monkeypatch):
        import app.services.llm as llm_mod

        calls = {"n": 0}
        def fake_retry(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 4:
                return "done", [], 1
            return "reading", [{"id": str(calls["n"]), "name": "read_file",
                               "args": {"path": f"/x{calls['n']}"}}], 1

        monkeypatch.setattr(llm_mod, "build_tool_definitions", lambda tools: [])
        monkeypatch.setattr(llm_mod, "retry_with_recovery", fake_retry)
        monkeypatch.setattr(AgentRuntime, "run_tool", lambda self, *a, **k: "ok")
        with app.app_context():
            agent = _real_agent(id="ra-loop2")
            db.session.add(agent)
            db.session.commit()
            try:
                rt = AgentRuntime(agent, provider={"base_url": "x", "api_key": "y", "model": "m"})
                tool_log = []
                reply = rt.chat_reply([{"role": "system", "content": "hi"}],
                                      max_tool_iters=10, tool_log=tool_log)
                assert reply == "done"
                assert len(tool_log) == 3   # all 3 distinct-arg calls ran, none refused
                assert not any(t["refused"] for t in tool_log)
            finally:
                from app.models.agent import TokenUsage
                TokenUsage.query.filter_by(agent_id="ra-loop2").delete()
                agent2 = db.session.get(type(agent), "ra-loop2")
                if agent2:
                    db.session.delete(agent2)
                db.session.commit()
