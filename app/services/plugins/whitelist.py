"""
Orion's Belt — Plugin Whitelist

Checks if a plugin is allowed to load via the `plugins.allowed` setting.

SECURE BY DEFAULT: a plugin in extensions/ is imported and executed with full
process privileges, so unknown plugins are DENIED unless explicitly allowed.
- Add plugin names to `plugins.allowed` (JSON array or comma-separated), OR
- Set `plugins.allow_all=true` to opt into loading every discovered plugin.

Setting format: JSON array of plugin names, e.g. ["my_plugin", "another_plugin"]
"""
import json
import logging

from app.models.settings import Setting

log = logging.getLogger("orions_belt.plugins.whitelist")

# Test injection point — set this to override the setting value
_test_value = None


def is_plugin_allowed(plugin_name: str) -> bool:
    """Check if a plugin is allowed to load.

    Deny-by-default: only plugins listed in `plugins.allowed` load, unless
    `plugins.allow_all=true` is set. A misconfigured whitelist fails closed.

    Args:
        plugin_name: The plugin name (filename without .py).

    Returns:
        True only if the plugin is explicitly allowed (or allow_all is on).
    """
    # Test injection point
    raw_value = _test_value if _test_value is not None else _get_setting_value()

    if not raw_value:
        # No whitelist configured — deny unless the operator opted into allow-all.
        if _allow_all_enabled():
            return True
        log.warning(
            "Plugin '%s' blocked: not in the plugins.allowed whitelist. Add it "
            "there, or set plugins.allow_all=true to load all plugins.",
            plugin_name,
        )
        return False

    try:
        # Try JSON first (array of strings)
        allowed = json.loads(raw_value)
        if isinstance(allowed, list):
            return plugin_name in allowed
        elif isinstance(allowed, str):
            # JSON string — treat as comma-separated
            names = [n.strip() for n in allowed.split(",")]
            return plugin_name in names
    except json.JSONDecodeError:
        # Not valid JSON — treat as comma-separated list
        names = [n.strip() for n in raw_value.split(",")]
        return plugin_name in names
    except Exception as e:
        log.warning("Plugin whitelist check failed for '%s': %s — denying", plugin_name, e)

    # Misconfigured whitelist → fail closed.
    return False


def _allow_all_enabled() -> bool:
    """True only if plugins.allow_all is explicitly truthy."""
    try:
        row = Setting.query.get("plugins.allow_all")
        return bool(row and str(row.value).strip().lower() in ("true", "1", "yes", "on"))
    except Exception:
        return False


def _get_setting_value() -> str | None:
    """Read the plugins.allowed setting value from the database."""
    try:
        row = Setting.query.get("plugins.allowed")
        return row.value if row else None
    except Exception as e:
        log.warning("Failed to read plugins.allowed setting: %s", e)
        return None
