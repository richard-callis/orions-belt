"""
Tests for the four MCP tools that were, until now, advertised via bundled
Novas (run_python, run_shell, fetch_url, http_request) but had zero backing
handler — calling any of them 404'd with "unknown or disabled tool". This
covers the real implementations added to close that gap.

run_python/run_shell are genuinely testable end-to-end (real subprocess, no
external dependency); fetch_url/http_request mock the HTTP layer the same
way the other connector tests in this suite do.
"""
import asyncio
import sys

import pytest

from app import db
from app.models.connector import AuthorizedDirectory
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestIsPrivateOrBlockedHost:
    def test_blocks_loopback_and_link_local(self):
        from app.services.connector_auth import is_private_or_blocked_host
        assert is_private_or_blocked_host("127.0.0.1") is True
        assert is_private_or_blocked_host("localhost") is True
        assert is_private_or_blocked_host("169.254.169.254") is True  # cloud metadata endpoint

    def test_blocks_private_rfc1918_unlike_is_blocked_host(self):
        from app.services.connector_auth import is_blocked_host, is_private_or_blocked_host
        assert is_blocked_host("10.0.0.5") is False       # connectors: intentionally allowed
        assert is_private_or_blocked_host("10.0.0.5") is True   # LLM-chosen URLs: blocked

    def test_allows_public_ip(self):
        from app.services.connector_auth import is_private_or_blocked_host
        assert is_private_or_blocked_host("8.8.8.8") is False


class TestValidateUntrustedUrl:
    def test_rejects_non_http_scheme(self):
        from app.services.connector_auth import validate_untrusted_url
        assert validate_untrusted_url("file:///etc/passwd") is not None
        assert validate_untrusted_url("ftp://example.com") is not None

    def test_rejects_private_host(self):
        from app.services.connector_auth import validate_untrusted_url
        assert validate_untrusted_url("http://192.168.1.1/") is not None

    def test_allows_public_https(self):
        from app.services.connector_auth import validate_untrusted_url
        assert validate_untrusted_url("https://example.com/page") is None


class _FakeStreamResponse:
    def __init__(self, status_code, headers, body: bytes):
        self.status_code = status_code
        self.headers = headers
        self.encoding = "utf-8"
        self._body = body

    async def aiter_bytes(self):
        yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeAsyncClient:
    def __init__(self, responses):
        # list of (status_code, headers, body_bytes), consumed in order
        self._responses = list(responses)

    def __call__(self, timeout=None, follow_redirects=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kwargs):
        status, headers, body = self._responses.pop(0)
        return _FakeStreamResponse(status, headers, body)


class TestFetchUrl:
    def test_fetches_and_returns_body(self, app, monkeypatch):
        import httpx
        client = _FakeAsyncClient([(200, {}, b"hello world")])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        with app.app_context():
            result = _run(mcp_tools._handle_fetch_url("fetch_url", {"url": "https://example.com"}))
        assert result == "hello world"

    def test_rejects_private_url_without_making_a_request(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_fetch_url("fetch_url", {"url": "http://10.0.0.1/"}))
        assert "not allowed" in result

    def test_requires_url(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_fetch_url("fetch_url", {}))
        assert "url is required" in result

    def test_follows_redirect_to_allowed_host(self, app, monkeypatch):
        import httpx
        client = _FakeAsyncClient([
            (302, {"location": "https://example.com/final"}, b""),
            (200, {}, b"final content"),
        ])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        with app.app_context():
            result = _run(mcp_tools._handle_fetch_url("fetch_url", {"url": "https://example.com/start"}))
        assert result == "final content"

    def test_blocks_redirect_to_private_host(self, app, monkeypatch):
        import httpx
        client = _FakeAsyncClient([
            (302, {"location": "http://169.254.169.254/latest/meta-data/"}, b""),
        ])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        with app.app_context():
            result = _run(mcp_tools._handle_fetch_url("fetch_url", {"url": "https://example.com/start"}))
        assert "redirect target blocked" in result

    def test_truncates_oversized_response(self, app, monkeypatch):
        import httpx
        huge = b"x" * (mcp_tools._MAX_HTTP_RESPONSE_BYTES + 1000)
        client = _FakeAsyncClient([(200, {}, huge)])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        with app.app_context():
            result = _run(mcp_tools._handle_fetch_url("fetch_url", {"url": "https://example.com"}))
        assert result.endswith("...[truncated]")
        assert len(result) <= mcp_tools._MAX_HTTP_RESPONSE_BYTES + len("\n...[truncated]") + 1


class TestHttpRequest:
    def test_get_request(self, app, monkeypatch):
        import httpx
        client = _FakeAsyncClient([(200, {"content-type": "application/json"}, b'{"ok": true}')])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        with app.app_context():
            result = _run(mcp_tools._handle_http_request("http_request", {
                "url": "https://example.com/api", "method": "GET",
            }))
        assert "HTTP 200" in result
        assert '{"ok": true}' in result

    def test_rejects_unsupported_method(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_http_request("http_request", {
                "url": "https://example.com", "method": "TRACE",
            }))
        assert "unsupported method" in result

    def test_rejects_private_url(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_http_request("http_request", {
                "url": "http://127.0.0.1:8080/admin", "method": "GET",
            }))
        assert "not allowed" in result

    def test_defaults_to_get(self, app, monkeypatch):
        import httpx
        client = _FakeAsyncClient([(200, {}, b"ok")])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        with app.app_context():
            result = _run(mcp_tools._handle_http_request("http_request", {"url": "https://example.com"}))
        assert "HTTP 200" in result


class TestRunPython:
    def test_executes_code_and_captures_stdout(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_run_python("run_python", {"code": "print('hi from python')"}))
        assert "exit_code=0" in result
        assert "hi from python" in result

    def test_captures_stderr_and_nonzero_exit(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_run_python("run_python", {"code": "import sys; sys.exit(3)"}))
        assert "exit_code=3" in result

    def test_requires_code(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_run_python("run_python", {}))
        assert "code is required" in result

    def test_times_out(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_run_python("run_python", {
                "code": "import time; time.sleep(5)", "timeout": 1,
            }))
        assert "timed out" in result

    def test_uses_a_real_interpreter_not_the_frozen_exe(self, app, monkeypatch):
        # Simulate a PyInstaller-frozen build: sys.executable would be
        # OrionsBelt.exe, not a Python interpreter, so _real_python_executable
        # must resolve a real one from PATH instead of using it directly.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        with app.app_context():
            result = _run(mcp_tools._handle_run_python("run_python", {"code": "print(1+1)"}))
        assert "exit_code=0" in result
        assert "2" in result

    def test_returns_error_when_frozen_and_no_interpreter_found(self, app, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(mcp_tools.shutil, "which", lambda name: None)
        with app.app_context():
            result = _run(mcp_tools._handle_run_python("run_python", {"code": "print(1)"}))
        assert "no Python interpreter" in result


class TestRunShell:
    def _make_dir(self, dir_id, path, enabled=True):
        d = AuthorizedDirectory(id=dir_id, path=path, alias=dir_id, enabled=enabled)
        db.session.add(d)
        db.session.commit()
        return d

    def test_requires_command(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_run_shell("run_shell", {}))
        assert "command is required" in result

    def test_runs_in_authorized_working_dir(self, app, tmp_path):
        with app.app_context():
            self._make_dir("d-shell1", str(tmp_path))
            try:
                result = _run(mcp_tools._handle_run_shell("run_shell", {
                    "command": "echo hello-shell", "working_dir": str(tmp_path),
                }))
                assert "exit_code=0" in result
                assert "hello-shell" in result
            finally:
                AuthorizedDirectory.query.filter_by(id="d-shell1").delete()
                db.session.commit()

    def test_rejects_unauthorized_working_dir(self, app, tmp_path):
        unauthorized = tmp_path / "not-authorized"
        unauthorized.mkdir()
        with app.app_context():
            result = _run(mcp_tools._handle_run_shell("run_shell", {
                "command": "echo x", "working_dir": str(unauthorized),
            }))
        assert "not authorized" in result

    def test_defaults_to_first_authorized_directory_when_none_given(self, app, tmp_path):
        with app.app_context():
            self._make_dir("d-shell2", str(tmp_path))
            try:
                result = _run(mcp_tools._handle_run_shell("run_shell", {"command": "echo default-dir"}))
                assert "exit_code=0" in result
                assert "default-dir" in result
            finally:
                AuthorizedDirectory.query.filter_by(id="d-shell2").delete()
                db.session.commit()

    def test_omitting_working_dir_does_not_bypass_read_only_cap(self, app, tmp_path):
        """Regression test: execute_tool's directory tier-cap check only
        inspects args the CALLER supplied — an omitted working_dir
        contributes nothing to it. _handle_run_shell's own fallback-
        directory resolution used to skip the read_only/max_tier check
        entirely, so a read-only directory being the only configured one
        could be bypassed simply by not passing working_dir at all."""
        with app.app_context():
            d = AuthorizedDirectory(id="d-shell-ro", path=str(tmp_path), alias="d-shell-ro",
                                    enabled=True, read_only=True)
            db.session.add(d)
            db.session.commit()
            try:
                omitted = _run(mcp_tools._handle_run_shell("run_shell", {"command": "echo x"}))
                assert omitted.startswith("Error")
                assert "only allows" in omitted
            finally:
                AuthorizedDirectory.query.filter_by(id="d-shell-ro").delete()
                db.session.commit()

    def test_omitting_working_dir_does_not_bypass_max_tier_cap(self, app, tmp_path):
        with app.app_context():
            d = AuthorizedDirectory(id="d-shell-cap", path=str(tmp_path), alias="d-shell-cap",
                                    enabled=True, max_tier=1)
            db.session.add(d)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_run_shell("run_shell", {"command": "echo x"}))
                assert result.startswith("Error")
                assert "only allows" in result
            finally:
                AuthorizedDirectory.query.filter_by(id="d-shell-cap").delete()
                db.session.commit()

    def test_errors_when_no_authorized_directories_and_none_given(self, app):
        with app.app_context():
            # Temporarily disable any existing enabled directories rather than
            # deleting them — this table is shared across the whole test
            # session, and an unscoped delete would wipe other test files'
            # fixtures out from under them.
            existing = AuthorizedDirectory.query.filter_by(enabled=True).all()
            existing_ids = [d.id for d in existing]
            for d in existing:
                d.enabled = False
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_run_shell("run_shell", {"command": "echo x"}))
                assert "no authorized directories" in result
            finally:
                for did in existing_ids:
                    d = AuthorizedDirectory.query.get(did)
                    if d:
                        d.enabled = True
                db.session.commit()

    def test_times_out(self, app, tmp_path):
        with app.app_context():
            self._make_dir("d-shell3", str(tmp_path))
            try:
                result = _run(mcp_tools._handle_run_shell("run_shell", {
                    "command": "sleep 5", "working_dir": str(tmp_path), "timeout": 1,
                }))
                assert "timed out" in result
            finally:
                AuthorizedDirectory.query.filter_by(id="d-shell3").delete()
                db.session.commit()
