"""
Tests for the send_email MCP tool (Outlook COM automation).
"""
import asyncio
import sys
import types

from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeMailItem:
    def __init__(self):
        self.To = None
        self.CC = None
        self.Subject = None
        self.Body = None
        self.sent = False

    def Send(self):
        self.sent = True


class _FakeOutlookApp:
    last_mail = None

    def CreateItem(self, item_type):
        mail = _FakeMailItem()
        _FakeOutlookApp.last_mail = mail
        return mail


def _install_fake_win32com():
    fake_client = types.ModuleType("win32com.client")
    fake_client.Dispatch = lambda name: _FakeOutlookApp()
    fake_win32com = types.ModuleType("win32com")
    fake_win32com.client = fake_client
    sys.modules["win32com"] = fake_win32com
    sys.modules["win32com.client"] = fake_client


class TestSendEmail:
    def test_sends_email_with_required_fields(self, app):
        _install_fake_win32com()
        try:
            with app.app_context():
                result = _run(mcp_tools._handle_send_email("send_email", {
                    "to": "someone@example.com", "subject": "Hi", "body": "Hello there",
                }))
            assert "Email sent to someone@example.com" in result
            assert _FakeOutlookApp.last_mail.sent is True
            assert _FakeOutlookApp.last_mail.To == "someone@example.com"
            assert _FakeOutlookApp.last_mail.Subject == "Hi"
            assert _FakeOutlookApp.last_mail.Body == "Hello there"
            assert _FakeOutlookApp.last_mail.CC is None
        finally:
            sys.modules.pop("win32com.client", None)
            sys.modules.pop("win32com", None)

    def test_cc_is_set_when_provided(self, app):
        _install_fake_win32com()
        try:
            with app.app_context():
                _run(mcp_tools._handle_send_email("send_email", {
                    "to": "a@example.com", "subject": "s", "body": "b", "cc": "b@example.com",
                }))
            assert _FakeOutlookApp.last_mail.CC == "b@example.com"
        finally:
            sys.modules.pop("win32com.client", None)
            sys.modules.pop("win32com", None)

    def test_requires_to(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_send_email("send_email", {"subject": "s", "body": "b"}))
        assert "'to' is required" in result

    def test_requires_subject(self, app):
        with app.app_context():
            result = _run(mcp_tools._handle_send_email("send_email", {"to": "a@example.com", "body": "b"}))
        assert "'subject' is required" in result

    def test_missing_pywin32_returns_clear_error(self, app, monkeypatch):
        # Simulate the package not being installed (e.g. non-Windows dev env).
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def blocking_import(name, *a, **k):
            if name == "win32com.client" or name.startswith("win32com"):
                raise ImportError("no win32com on this platform")
            return real_import(name, *a, **k)

        monkeypatch.setattr("builtins.__import__", blocking_import)
        with app.app_context():
            result = _run(mcp_tools._handle_send_email("send_email", {
                "to": "a@example.com", "subject": "s", "body": "b",
            }))
        assert "pywin32 not installed" in result
