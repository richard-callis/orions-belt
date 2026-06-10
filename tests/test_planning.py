"""
Tests for plan-before-execute gate, wave scheduling, and task dependencies.
"""
import pytest
from app.services.agents import _extract_plan


class TestExtractPlan:
    def test_no_plan_block(self):
        assert _extract_plan("Just doing the task now.") is None

    def test_low_risk_plan(self):
        text = "<plan><risk_level>low</risk_level></plan>"
        result = _extract_plan(text)
        assert result is not None
        assert result["risk_level"] == "low"

    def test_high_risk_plan(self):
        text = (
            "<plan>\n"
            "  <risk_level>high</risk_level>\n"
            "  <verify_step>Check backup exists</verify_step>\n"
            "  <rollback_step>Restore from backup</rollback_step>\n"
            "</plan>"
        )
        result = _extract_plan(text)
        assert result is not None
        assert result["risk_level"] == "high"
        assert result["verify_steps"] == ["Check backup exists"]
        assert result["rollback_steps"] == ["Restore from backup"]

    def test_critical_risk_plan(self):
        text = "<plan><risk_level>CRITICAL</risk_level></plan>"
        result = _extract_plan(text)
        assert result["risk_level"] == "critical"

    def test_multiple_verify_steps(self):
        text = (
            "<plan><risk_level>high</risk_level>"
            "<verify_step>Step 1</verify_step>"
            "<verify_step>Step 2</verify_step></plan>"
        )
        result = _extract_plan(text)
        assert len(result["verify_steps"]) == 2

    def test_plan_with_surrounding_text(self):
        text = "I will now execute the plan.\n<plan><risk_level>medium</risk_level></plan>\nProceeding."
        result = _extract_plan(text)
        assert result["risk_level"] == "medium"

    def test_raw_xml_preserved(self):
        text = "<plan><risk_level>low</risk_level></plan>"
        result = _extract_plan(text)
        assert "<plan>" in result["raw_xml"]


class TestWaveScheduling:
    """Test topological wave computation via the work routes helper."""

    def test_compute_waves_no_deps(self, app, db):
        """Tasks with no dependencies all land on wave 0."""
        import uuid
        from app import db as _db
        from app.models.work import Project, Epic, Feature, Task
        from app.routes.work import _compute_waves

        with app.app_context():
            p = Project(id=str(uuid.uuid4()), name="Test Project")
            e = Epic(id=str(uuid.uuid4()), project_id=p.id, title="E")
            f = Feature(id=str(uuid.uuid4()), epic_id=e.id, title="F")
            t1 = Task(id=str(uuid.uuid4()), feature_id=f.id, title="T1")
            t2 = Task(id=str(uuid.uuid4()), feature_id=f.id, title="T2")
            for obj in (p, e, f, t1, t2):
                _db.session.add(obj)
            _db.session.flush()

            _compute_waves(f.id)
            assert t1.wave == 0
            assert t2.wave == 0

    def test_compute_waves_linear_chain(self, app, db):
        """T1 → T2 → T3 should produce waves 0, 1, 2."""
        import uuid
        from app import db as _db
        from app.models.work import Project, Epic, Feature, Task
        from app.routes.work import _compute_waves

        with app.app_context():
            p = Project(id=str(uuid.uuid4()), name="Test Project 2")
            e = Epic(id=str(uuid.uuid4()), project_id=p.id, title="E2")
            f = Feature(id=str(uuid.uuid4()), epic_id=e.id, title="F2")
            t1 = Task(id=str(uuid.uuid4()), feature_id=f.id, title="T1")
            t2 = Task(id=str(uuid.uuid4()), feature_id=f.id, title="T2")
            t3 = Task(id=str(uuid.uuid4()), feature_id=f.id, title="T3")
            for obj in (p, e, f, t1, t2, t3):
                _db.session.add(obj)
            _db.session.flush()

            t2.depends_on = [t1.id]
            t3.depends_on = [t2.id]
            _db.session.flush()

            _compute_waves(f.id)
            assert t1.wave == 0
            assert t2.wave == 1
            assert t3.wave == 2
