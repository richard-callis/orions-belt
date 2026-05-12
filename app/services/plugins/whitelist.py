"""
Orion's Belt — Plugin Whitelist

Checks if a plugin is allowed to load via the `plugins.allowed` setting.
Default: allow all (backward compatible).

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

    Checks the `plugins.allowed` Setting. If the setting is not set or is
    empty, all plugins are allowed (opt-in model).

    Args:
        plugin_name: The plugin name (filename without .py).

    Returns:
        True if plugin is allowed or no whitelist is configured.
    """
    # Test injection point
    raw_value = _test_value if _test_value is not None else _get_setting_value()

    if not raw_value:
        # No whitelist configured — allow all
        return True

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
        log.warning("Plugin whitelist check failed for '%s': %s", plugin_name, e)

    # If whitelist is misconfigured, allow all (backward compatible)
    return True


def _get_setting_value() -> str | None:
    """Read the plugins.allowed setting value from the database."""
    try:
        row = Setting.query.get("plugins.allowed")
        return row.value if row else None
    except Exception as e:
        log.warning("Failed to read plugins.allowed setting: %s", e)
        return None
