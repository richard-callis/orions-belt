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
        self.HTMLBody = None
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


class _FakePythoncom:
    """Tracks CoInitialize/CoUninitialize call order so tests can assert
    they're actually invoked (and paired) around the COM calls, without
    needing a real Windows COM apartment."""
    calls = []

    @staticmethod
    def CoInitialize():
        _FakePythoncom.calls.append("init")

    @staticmethod
    def CoUninitialize():
        _FakePythoncom.calls.append("uninit")


def _install_fake_pythoncom():
    _FakePythoncom.calls = []
    sys.modules["pythoncom"] = _FakePythoncom


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

    def test_html_body_used_instead_of_plain_body_when_given(self, app):
        _install_fake_win32com()
        try:
            with app.app_context():
                _run(mcp_tools._handle_send_email("send_email", {
                    "to": "a@example.com", "subject": "s", "body": "plain fallback",
                    "html": "<p>rich content</p>",
                }))
            assert _FakeOutlookApp.last_mail.HTMLBody == "<p>rich content</p>"
            assert _FakeOutlookApp.last_mail.Body is None  # Body never set when html is given
        finally:
            sys.modules.pop("win32com.client", None)
            sys.modules.pop("win32com", None)

    def test_plain_body_used_when_no_html_given(self, app):
        _install_fake_win32com()
        try:
            with app.app_context():
                _run(mcp_tools._handle_send_email("send_email", {
                    "to": "a@example.com", "subject": "s", "body": "plain text",
                }))
            assert _FakeOutlookApp.last_mail.Body == "plain text"
            assert _FakeOutlookApp.last_mail.HTMLBody is None
        finally:
            sys.modules.pop("win32com.client", None)
            sys.modules.pop("win32com", None)

    def test_com_initialize_and_uninitialize_are_called_when_pythoncom_available(self, app):
        # A scheduled digest fires from a dedicated scheduler thread that has
        # never exercised this COM path before — CoInitialize/CoUninitialize
        # must actually run, not just be assumed safe to skip.
        _install_fake_win32com()
        _install_fake_pythoncom()
        try:
            with app.app_context():
                _run(mcp_tools._handle_send_email("send_email", {
                    "to": "a@example.com", "subject": "s", "body": "b",
                }))
            assert _FakePythoncom.calls == ["init", "uninit"]
        finally:
            sys.modules.pop("win32com.client", None)
            sys.modules.pop("win32com", None)
            sys.modules.pop("pythoncom", None)

    def test_com_uninitialize_still_runs_when_send_raises(self, app):
        _install_fake_win32com()
        _install_fake_pythoncom()

        class _RaisingMailItem(_FakeMailItem):
            def Send(self):
                raise RuntimeError("simulated COM failure")

        class _RaisingOutlookApp:
            def CreateItem(self, item_type):
                return _RaisingMailItem()

        import win32com.client as fake_client
        fake_client.Dispatch = lambda name: _RaisingOutlookApp()

        try:
            with app.app_context():
                result = _run(mcp_tools._handle_send_email("send_email", {
                    "to": "a@example.com", "subject": "s", "body": "b",
                }))
            assert "Error sending email" in result
            assert _FakePythoncom.calls == ["init", "uninit"]
        finally:
            sys.modules.pop("win32com.client", None)
            sys.modules.pop("win32com", None)
            sys.modules.pop("pythoncom", None)

    def test_com_already_initialized_in_different_mode_is_benign(self, app):
        # CoInitialize() raises pythoncom.com_error (RPC_E_CHANGED_MODE) if
        # the calling thread already has a COM apartment in a DIFFERENT
        # threading mode — a real possibility for existing callers (request
        # threads, agent background threads) that ran other COM code first.
        # This must not propagate as a tool failure; the thread already has
        # SOME apartment, which is all Dispatch() needs.
        _install_fake_win32com()

        class _RaisingPythoncom:
            calls = []

            @staticmethod
            def CoInitialize():
                _RaisingPythoncom.calls.append("init-attempted")
                raise OSError("simulated RPC_E_CHANGED_MODE: thread already initialized")

            @staticmethod
            def CoUninitialize():
                _RaisingPythoncom.calls.append("uninit")

        _RaisingPythoncom.calls = []
        sys.modules["pythoncom"] = _RaisingPythoncom
        try:
            with app.app_context():
                result = _run(mcp_tools._handle_send_email("send_email", {
                    "to": "a@example.com", "subject": "s", "body": "b",
                }))
            assert "Email sent" in result
            # CoInitialize was attempted (and failed) but since we never
            # successfully initialized, we must not call CoUninitialize —
            # that would tear down an apartment we didn't set up.
            assert _RaisingPythoncom.calls == ["init-attempted"]
        finally:
            sys.modules.pop("win32com.client", None)
            sys.modules.pop("win32com", None)
            sys.modules.pop("pythoncom", None)

    def test_works_without_pythoncom_installed(self, app):
        # pythoncom must be a fully optional import — the tool still works
        # (just without the explicit COM init) when it isn't present, e.g.
        # on any non-Windows dev/CI environment.
        _install_fake_win32com()
        sys.modules.pop("pythoncom", None)
        try:
            with app.app_context():
                result = _run(mcp_tools._handle_send_email("send_email", {
                    "to": "a@example.com", "subject": "s", "body": "b",
                }))
            assert "Email sent" in result
        finally:
            sys.modules.pop("win32com.client", None)
            sys.modules.pop("win32com", None)

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
