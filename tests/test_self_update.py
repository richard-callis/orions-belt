"""
Tests for app/services/self_update.py and its /api/system/update/* routes.

apply_update() is scoped to from-source installs only: it refuses on a
frozen build, refuses on an unclean working tree (a plain `git pull` would
either fail or silently merge over uncommitted local edits), and on
success schedules a full process restart via os.execv. That last part is
never allowed to actually run in tests — _schedule_restart is always
mocked, since a real os.execv would kill the test process outright.
"""
from unittest.mock import MagicMock, patch

import pytest

import app.services.self_update as su


# ── Version parsing / comparison ────────────────────────────────────────────

class TestVersionCompare:
    def test_parses_v_prefixed_semver(self):
        assert su._parse_version("v1.2.0") == (1, 2, 0)

    def test_parses_bare_semver(self):
        assert su._parse_version("1.2.0") == (1, 2, 0)

    def test_short_version_padded_with_zeros(self):
        assert su._parse_version("v2") == (2, 0, 0)

    def test_non_numeric_suffix_does_not_raise(self):
        assert su._parse_version("v1.2.0-rc1") == (1, 2, 0)

    def test_is_newer_true_for_higher_version(self):
        assert su.is_newer("v1.3.0", "v1.2.0") is True

    def test_is_newer_false_for_equal_version(self):
        assert su.is_newer("v1.2.0", "v1.2.0") is False

    def test_is_newer_false_for_lower_version(self):
        assert su.is_newer("v1.1.0", "v1.2.0") is False


# ── is_source_install / get_current_version ────────────────────────────────

class TestSourceInstallDetection:
    def test_true_when_git_dir_present_and_not_frozen(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(su, "ROOT", tmp_path)
        monkeypatch.setattr(su.sys, "frozen", False, raising=False)
        assert su.is_source_install() is True

    def test_false_when_no_git_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su, "ROOT", tmp_path)
        monkeypatch.setattr(su.sys, "frozen", False, raising=False)
        assert su.is_source_install() is False

    def test_false_when_frozen_even_with_git_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(su, "ROOT", tmp_path)
        monkeypatch.setattr(su.sys, "frozen", True, raising=False)
        assert su.is_source_install() is False


class TestGetCurrentVersion:
    def test_uses_git_describe_for_source_install(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(su, "ROOT", tmp_path)
        monkeypatch.setattr(su.sys, "frozen", False, raising=False)
        fake = MagicMock(returncode=0, stdout="v1.2.0\n")
        with patch.object(su.subprocess, "run", return_value=fake) as run:
            assert su.get_current_version() == "v1.2.0"
        run.assert_called_once()

    def test_falls_back_to_config_when_not_source_install(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su, "ROOT", tmp_path)  # no .git dir
        monkeypatch.setattr(su.sys, "frozen", False, raising=False)
        assert su.get_current_version() == su.Config.APP_VERSION

    def test_falls_back_when_git_describe_fails(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(su, "ROOT", tmp_path)
        monkeypatch.setattr(su.sys, "frozen", False, raising=False)
        fake = MagicMock(returncode=1, stdout="")
        with patch.object(su.subprocess, "run", return_value=fake):
            assert su.get_current_version() == su.Config.APP_VERSION


# ── check_for_update ────────────────────────────────────────────────────────

class TestCheckForUpdate:
    def test_reports_update_available(self, monkeypatch):
        monkeypatch.setattr(su, "get_current_version", lambda: "v1.2.0")
        monkeypatch.setattr(su, "is_source_install", lambda: True)
        fake_resp = MagicMock()
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: {
            "tag_name": "v1.3.0", "html_url": "https://example/release",
            "body": "notes", "published_at": "2026-01-01T00:00:00Z",
        }
        with patch.object(su.requests, "get", return_value=fake_resp):
            result = su.check_for_update()
        assert result["ok"] is True
        assert result["update_available"] is True
        assert result["latest_version"] == "v1.3.0"
        assert result["current_version"] == "v1.2.0"
        assert result["source_install"] is True

    def test_reports_up_to_date(self, monkeypatch):
        monkeypatch.setattr(su, "get_current_version", lambda: "v1.3.0")
        monkeypatch.setattr(su, "is_source_install", lambda: True)
        fake_resp = MagicMock()
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: {"tag_name": "v1.3.0", "html_url": "u", "body": "", "published_at": ""}
        with patch.object(su.requests, "get", return_value=fake_resp):
            result = su.check_for_update()
        assert result["ok"] is True
        assert result["update_available"] is False

    def test_network_failure_returns_soft_error(self, monkeypatch):
        monkeypatch.setattr(su, "get_current_version", lambda: "v1.2.0")
        monkeypatch.setattr(su, "is_source_install", lambda: True)
        with patch.object(su.requests, "get", side_effect=OSError("no network")):
            result = su.check_for_update()
        assert result["ok"] is False
        assert "current_version" in result


# ── apply_update ─────────────────────────────────────────────────────────────

def _cp(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestApplyUpdate:
    def test_refuses_when_not_source_install(self, monkeypatch):
        monkeypatch.setattr(su, "is_source_install", lambda: False)
        result = su.apply_update()
        assert result["ok"] is False
        assert "source install" in result["error"].lower()

    def test_refuses_on_dirty_working_tree(self, monkeypatch):
        monkeypatch.setattr(su, "is_source_install", lambda: True)
        with patch.object(su.subprocess, "run", return_value=_cp(stdout=" M app/foo.py\n")):
            result = su.apply_update()
        assert result["ok"] is False
        assert "local changes" in result["error"].lower()

    def test_refuses_when_git_pull_fails(self, monkeypatch):
        monkeypatch.setattr(su, "is_source_install", lambda: True)
        calls = [_cp(stdout=""), _cp(returncode=1, stderr="fatal: not a fast-forward")]
        with patch.object(su.subprocess, "run", side_effect=calls):
            result = su.apply_update()
        assert result["ok"] is False
        assert "not a fast-forward" in result["error"]

    def test_success_without_dependency_changes_does_not_reinstall(self, monkeypatch, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("flask>=3.0.0\n")
        monkeypatch.setattr(su, "ROOT", tmp_path)
        monkeypatch.setattr(su, "is_source_install", lambda: True)
        calls = [_cp(stdout=""), _cp(returncode=0)]  # git status, git pull
        run_mock = MagicMock(side_effect=calls)
        restart_mock = MagicMock()
        with patch.object(su.subprocess, "run", run_mock), \
             patch.object(su, "_schedule_restart", restart_mock):
            result = su.apply_update()
        assert result == {"ok": True, "deps_reinstalled": False, "restarting": True}
        assert run_mock.call_count == 2  # no pip install call
        restart_mock.assert_called_once()

    def test_success_with_dependency_changes_reinstalls(self, monkeypatch, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("flask>=3.0.0\n")
        monkeypatch.setattr(su, "ROOT", tmp_path)
        monkeypatch.setattr(su, "is_source_install", lambda: True)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "status"]:
                return _cp(stdout="")
            if cmd[:2] == ["git", "pull"]:
                req.write_text("flask>=3.1.0\n")  # simulate requirements.txt changing
                return _cp(returncode=0)
            if "pip" in cmd:
                return _cp(returncode=0)
            raise AssertionError(f"unexpected command: {cmd}")

        with patch.object(su.subprocess, "run", side_effect=fake_run), \
             patch.object(su, "_schedule_restart") as restart_mock:
            result = su.apply_update()
        assert result == {"ok": True, "deps_reinstalled": True, "restarting": True}
        restart_mock.assert_called_once()

    def test_pip_install_failure_reports_error_and_does_not_restart(self, monkeypatch, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("flask>=3.0.0\n")
        monkeypatch.setattr(su, "ROOT", tmp_path)
        monkeypatch.setattr(su, "is_source_install", lambda: True)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "status"]:
                return _cp(stdout="")
            if cmd[:2] == ["git", "pull"]:
                req.write_text("flask>=3.1.0\n")
                return _cp(returncode=0)
            if "pip" in cmd:
                return _cp(returncode=1, stderr="Could not find a version that satisfies")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch.object(su.subprocess, "run", side_effect=fake_run), \
             patch.object(su, "_schedule_restart") as restart_mock:
            result = su.apply_update()
        assert result["ok"] is False
        assert "dependency install failed" in result["error"]
        restart_mock.assert_not_called()


# ── Routes ───────────────────────────────────────────────────────────────────

class TestUpdateRoutes:
    def test_check_route_returns_service_result(self, client):
        fake_result = {"ok": True, "current_version": "v1.2.0", "latest_version": "v1.2.0",
                        "update_available": False, "source_install": True}
        with patch("app.routes.system.check_for_update", return_value=fake_result):
            resp = client.get("/api/system/update/check")
        assert resp.status_code == 200
        assert resp.get_json() == fake_result

    def test_apply_route_returns_200_on_success(self, client):
        fake_result = {"ok": True, "deps_reinstalled": False, "restarting": True}
        with patch("app.routes.system.apply_update", return_value=fake_result):
            resp = client.post("/api/system/update/apply")
        assert resp.status_code == 200
        assert resp.get_json() == fake_result

    def test_apply_route_returns_400_on_failure(self, client):
        fake_result = {"ok": False, "error": "Not a source install — download the latest release instead."}
        with patch("app.routes.system.apply_update", return_value=fake_result):
            resp = client.post("/api/system/update/apply")
        assert resp.status_code == 400
        assert resp.get_json() == fake_result

    def test_health_route_reports_version(self, client):
        with patch("app.routes.system.get_current_version", return_value="v1.2.0"):
            resp = client.get("/api/system/health")
        assert resp.get_json()["version"] == "v1.2.0"
