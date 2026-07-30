"""
Tests for AgentRuntime — the shared 'an agent runs its tools' capability.
"""
from types import SimpleNamespace

from app import db
from app.models.connector import AuthorizedDirectory
from app.models.mcp_tool import MCPTool
from app.services.agents.runtime import AgentRuntime


def _agent(allowed="[]"):
    return SimpleNamespace(id="a1", name="A1", allowed_tools=allowed, llm_model_override=None)


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
