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
