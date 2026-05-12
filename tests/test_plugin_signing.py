"""Tests for plugin signing and whitelist."""
import shutil
from pathlib import Path

import pytest


_SIGNING_DIR = Path(__file__).parent.parent / ".plugin_signing_key"


@pytest.fixture(autouse=True)
def clean_signing_dir():
    """Clean signing keys between tests so each test gets fresh keys."""
    if _SIGNING_DIR.exists():
        shutil.rmtree(_SIGNING_DIR)
    yield
    if _SIGNING_DIR.exists():
        shutil.rmtree(_SIGNING_DIR)


class TestPluginSigning:
    def test_sign_and_verify(self, tmp_path):
        """A signed plugin should verify successfully."""
        from app.services.plugins.signing import sign_plugin, verify_plugin

        plugin_path = tmp_path / "test_plugin.py"
        plugin_path.write_text("# test plugin\n")

        sign_plugin(plugin_path)
        sig_path = tmp_path / "test_plugin.py.sig"
        assert sig_path.exists()

        assert verify_plugin(plugin_path) is True

    def test_tampered_plugin_fails_verification(self, tmp_path):
        """A tampered plugin should fail signature verification."""
        from app.services.plugins.signing import sign_plugin, verify_plugin

        plugin_path = tmp_path / "test_plugin.py"
        plugin_path.write_text("# original content\n")

        sign_plugin(plugin_path)

        # Tamper with the plugin
        plugin_path.write_text("# tampered content\n")

        assert verify_plugin(plugin_path) is False

    def test_unsigned_plugin_passes(self, tmp_path):
        """Unsigned plugins should pass (opt-in model)."""
        from app.services.plugins.signing import verify_plugin

        plugin_path = tmp_path / "unsigned.py"
        plugin_path.write_text("# no signature\n")

        assert verify_plugin(plugin_path) is True


class TestPluginWhitelist:
    """Uses _test_value injection point on the whitelist module."""

    def _inject(self, value):
        import app.services.plugins.whitelist as wl
        wl._test_value = value

    def _clear(self):
        import app.services.plugins.whitelist as wl
        wl._test_value = None

    def test_no_whitelist_allows_all(self):
        self._inject(None)
        from app.services.plugins.whitelist import is_plugin_allowed
        assert is_plugin_allowed("any") is True
        self._clear()

    def test_list_allows_named(self):
        self._inject('["a", "b"]')
        from app.services.plugins.whitelist import is_plugin_allowed
        assert is_plugin_allowed("a") is True
        assert is_plugin_allowed("b") is True
        assert is_plugin_allowed("c") is False
        self._clear()

    def test_comma_separated(self):
        self._inject("x, y")
        from app.services.plugins.whitelist import is_plugin_allowed
        assert is_plugin_allowed("x") is True
        assert is_plugin_allowed("y") is True
        assert is_plugin_allowed("z") is False
        self._clear()
