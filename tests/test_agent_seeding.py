"""
Tests for launch._seed_agents — default live Agent rows shipped with the app.
"""
import json

import pytest

from app import db
from app.models.agent import Agent
from launch import _seed_agents

_DEFAULT_NAMES = {"Project Planner", "Email Assistant", "Document Writer", "Research Assistant"}


@pytest.fixture(autouse=True)
def _cleanup_seeded_agents(app):
    # The `app` fixture's DB is session-scoped, so leftover seeded rows would
    # otherwise persist and pollute every test file that runs afterward.
    yield
    with app.app_context():
        Agent.query.filter(Agent.name.in_(_DEFAULT_NAMES)).delete(synchronize_session=False)
        db.session.commit()


class TestSeedAgents:
    def test_creates_all_default_agents(self, app):
        with app.app_context():
            _seed_agents(app)
            names = {a.name for a in Agent.query.filter(Agent.name.in_(_DEFAULT_NAMES)).all()}
            assert names == _DEFAULT_NAMES

    def test_idempotent_on_repeat_calls(self, app):
        with app.app_context():
            _seed_agents(app)
            _seed_agents(app)
            for name in _DEFAULT_NAMES:
                count = Agent.query.filter_by(name=name).count()
                assert count == 1, f"{name} seeded {count} times"

    def test_document_writer_has_office_doc_tools(self, app):
        with app.app_context():
            _seed_agents(app)
            agent = Agent.query.filter_by(name="Document Writer").first()
            tools = json.loads(agent.allowed_tools)
            for t in ("create_word_document", "create_powerpoint", "create_excel", "create_pdf"):
                assert t in tools

    def test_project_planner_prompt_is_nonempty(self, app):
        with app.app_context():
            _seed_agents(app)
            agent = Agent.query.filter_by(name="Project Planner").first()
            assert agent.system_prompt and len(agent.system_prompt) > 20

    def test_email_assistant_is_read_only(self, app):
        with app.app_context():
            _seed_agents(app)
            agent = Agent.query.filter_by(name="Email Assistant").first()
            assert json.loads(agent.allowed_tools) == ["search_emails"]

    def test_does_not_touch_a_user_renamed_agent(self, app):
        # A user could have an unrelated agent that happens to share a name
        # with something else — seeding must only skip on an EXACT name match,
        # never overwrite an existing row's fields.
        with app.app_context():
            custom = Agent(name="Project Planner", system_prompt="custom prompt",
                            allowed_tools=json.dumps(["read_file"]))
            db.session.add(custom)
            db.session.commit()
            try:
                _seed_agents(app)
                agent = Agent.query.filter_by(name="Project Planner").first()
                assert agent.system_prompt == "custom prompt"
            finally:
                Agent.query.filter_by(name="Project Planner").delete()
                db.session.commit()
