"""
Tests for AgentRuntime — the shared 'an agent runs its tools' capability.
"""
from types import SimpleNamespace

from app import db
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
        import app.routes.chat as chat_routes
        monkeypatch.setattr(chat_routes, "_run_tool", lambda name, args, **k: f"ran:{name}")
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
