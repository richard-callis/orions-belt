"""
Tests for install.py, the single cross-platform installer/launcher that
replaced the previous separate setup.sh/setup.bat + run.sh/run.bat pairs.

No Flask app context needed here — install.py is a standalone script with
no dependency on the app package, so these tests don't use the `app`/`db`
fixtures. Model downloads and full venv installs aren't exercised here
(too slow/network-dependent for the unit suite); see the manual smoke test
in the PR description for an end-to-end venv-creation + pip-install check.
"""
import io
import sys

import install as install_mod


class TestSslErrorDetection:
    def test_detects_ssl_certificate_errors(self):
        assert install_mod._looks_like_ssl_error("SSL: CERTIFICATE_VERIFY_FAILED") is True
        assert install_mod._looks_like_ssl_error("certificate verify failed") is True
        assert install_mod._looks_like_ssl_error("ssl.SSLCertVerificationError") is True

    def test_does_not_flag_unrelated_pip_failures(self):
        assert install_mod._looks_like_ssl_error(
            "ERROR: Could not find a version that satisfies the requirement foo"
        ) is False
        assert install_mod._looks_like_ssl_error("Permission denied") is False


class TestSslBypassPrompt:
    def test_declines_gracefully_on_non_interactive_stdin(self, monkeypatch):
        """A CI/non-tty environment has no stdin to read — input() would
        raise EOFError. Must decline (not hang, not crash) rather than
        propagate that exception up through a pip_install() call."""
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert install_mod._ask_ssl_bypass() is False

    def test_declines_on_explicit_no(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
        assert install_mod._ask_ssl_bypass() is False

    def test_accepts_on_yes(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
        assert install_mod._ask_ssl_bypass() is True


class TestVenvPaths:
    def test_venv_python_path_matches_platform(self, monkeypatch):
        monkeypatch.setattr(install_mod, "IS_WINDOWS", True)
        assert str(install_mod.venv_python()).endswith(("Scripts/python.exe", "Scripts\\python.exe"))

        monkeypatch.setattr(install_mod, "IS_WINDOWS", False)
        assert str(install_mod.venv_python()).endswith("bin/python")

    def test_venv_exists_reflects_python_binary_presence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install_mod, "VENV_DIR", tmp_path / ".venv")
        assert install_mod.venv_exists() is False

        python_path = install_mod.venv_python()
        python_path.parent.mkdir(parents=True)
        python_path.touch()
        assert install_mod.venv_exists() is True


class TestFindSystemPython:
    def test_rejects_python_older_than_minimum(self, monkeypatch):
        monkeypatch.setattr(install_mod.sys, "version_info", (3, 9, 0))
        try:
            install_mod.find_system_python()
            assert False, "expected SystemExit for an out-of-date interpreter"
        except SystemExit as e:
            assert "3.11" in str(e)


class TestCreateVenv:
    def test_recreates_a_half_built_venv_instead_of_treating_it_as_done(self, tmp_path, monkeypatch):
        """Regression test: create_venv() used to check VENV_DIR.exists()
        (the directory) instead of venv_exists() (the python binary inside
        it). A .venv directory can exist without a working interpreter —
        e.g. venv creation was interrupted partway — and the old check would
        treat that half-built venv as "already done", skip straight to
        pip_install, and fail with an unhandled FileNotFoundError the first
        time it tried to run the nonexistent venv python."""
        monkeypatch.setattr(install_mod, "VENV_DIR", tmp_path / ".venv")
        # Simulate an interrupted venv: the directory exists, but there's no
        # python binary inside it yet.
        (tmp_path / ".venv").mkdir()
        assert install_mod.venv_exists() is False

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _FakeCompletedProcess()

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
        install_mod.create_venv()
        assert len(calls) == 1, "a half-built .venv must trigger an actual venv-creation call, not be skipped"

    def test_does_not_recreate_a_genuinely_complete_venv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install_mod, "VENV_DIR", tmp_path / ".venv")
        python_path = install_mod.venv_python()
        python_path.parent.mkdir(parents=True)
        python_path.touch()
        assert install_mod.venv_exists() is True

        calls = []
        monkeypatch.setattr(install_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        install_mod.create_venv()
        assert calls == [], "a complete .venv must be reused, not recreated"


class _FakeCompletedProcess:
    returncode = 0


class TestSslBypassTrustedHosts:
    def test_bypass_includes_pytorch_host(self, monkeypatch):
        """Regression test: the old setup.bat's PyTorch-specific SSL-bypass
        retry included --trusted-host download.pytorch.org (in addition to
        pypi.org/files.pythonhosted.org) — omitting it here means a proxy
        that intercepts pytorch.org specifically would still fail the CPU
        wheel install even after the user approves the bypass, since
        pip_install() is the one shared helper used for every install
        (including the PyTorch step)."""
        monkeypatch.setattr(install_mod, "_ssl_bypass", True)
        captured = {}

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeResult()

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
        install_mod.pip_install(["torch==2.7.1+cpu", "--index-url",
                                 "https://download.pytorch.org/whl/cpu"])
        assert "download.pytorch.org" in captured["cmd"]
