"""
Tests for the agent-run resume-after-approval logic.

When a Tier-3 step is approved, the run loop must EXECUTE the approved step on
resume rather than creating a new pending step and pausing again (the previous
infinite-approval-loop bug).
"""
import uuid

from app import db
from app.models.agent import Agent, AgentRun, AgentStep
from app.services.agents import (
    _find_approved_pending_step,
    _find_checkpointed_step,
    _compute_checkpoint_hash,
)


def _make_run(app):
    agent = Agent(id=str(uuid.uuid4()), name="a", status="running")
    run = AgentRun(id=str(uuid.uuid4()), agent_id=agent.id, task_id="t1", status="awaiting_approval")
    db.session.add_all([agent, run])
    db.session.commit()
    return run


class TestResumeLookup:
    def test_finds_approved_but_unexecuted_step(self, app, client):
        with app.app_context():
            run = _make_run(app)
            chk = _compute_checkpoint_hash("delete_file", {"path": "/tmp/x"})
            step = AgentStep(
                id=str(uuid.uuid4()), run_id=run.id, step_number=0,
                tool_name="delete_file", tool_input="{}",
                required_approval=True, approved=True, checkpoint_hash=chk,
                is_checkpointed=False,
            )
            db.session.add(step)
            db.session.commit()

            found = _find_approved_pending_step(run.id, chk)
            assert found is not None
            assert found.id == step.id

    def test_ignores_already_executed_step(self, app, client):
        with app.app_context():
            run = _make_run(app)
            chk = _compute_checkpoint_hash("delete_file", {"path": "/tmp/y"})
            step = AgentStep(
                id=str(uuid.uuid4()), run_id=run.id, step_number=0,
                tool_name="delete_file", tool_input="{}",
                required_approval=True, approved=True, checkpoint_hash=chk,
                is_checkpointed=True,  # already executed
            )
            db.session.add(step)
            db.session.commit()

            # Not a pending step anymore; it's a checkpoint instead.
            assert _find_approved_pending_step(run.id, chk) is None
            assert _find_checkpointed_step(run.id, chk) is not None

    def test_ignores_unapproved_pending_step(self, app, client):
        with app.app_context():
            run = _make_run(app)
            chk = _compute_checkpoint_hash("delete_file", {"path": "/tmp/z"})
            step = AgentStep(
                id=str(uuid.uuid4()), run_id=run.id, step_number=0,
                tool_name="delete_file", tool_input="{}",
                required_approval=True, approved=None, checkpoint_hash=chk,
                is_checkpointed=False,
            )
            db.session.add(step)
            db.session.commit()

            assert _find_approved_pending_step(run.id, chk) is None
