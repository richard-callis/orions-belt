"""Tool availability checks — filter tools based on platform and dependencies.

Some tools (e.g., search_emails) require Windows + pyodbc, others may need
specific Python packages or OS features. This module gates tool visibility
so the LLM only sees tools that can actually execute.
"""
import logging
import platform
import sys

log = logging.getLogger("orions-belt")

# Cache availability results — they don't change during a process lifetime
_cache: dict[str, bool] = {}


def is_tool_available(tool_name: str, enabled: bool = True) -> bool:
    """Check if a tool is available on this platform.

    Args:
        tool_name: The MCP tool name (e.g., "search_emails", "run_sql_query")
        enabled: Whether the tool is marked enabled in the DB

    Returns:
        True if the tool can be executed on this system
    """
    if not enabled:
        return False

    if tool_name in _cache:
        return _cache[tool_name]

    available = _check_availability(tool_name)
    _cache[tool_name] = available

    if not available:
        log.info("tool.unavailable name=%s (platform/dependency not met)", tool_name)

    return available


def _check_availability(tool_name: str) -> bool:
    """Internal platform/dependency check for specific tools."""
    is_windows = platform.system() == "Windows"

    # search_emails requires Windows + pyodbc + Outlook MAPI
    if tool_name == "search_emails":
        if not is_windows:
            return False
        try:
            import pyodbc  # noqa: F401
            return True
        except ImportError:
            return False

    # run_sql_query requires pyodbc
    if tool_name == "run_sql_query":
        try:
            import pyodbc  # noqa: F401
            return True
        except ImportError:
            return False

    # All other tools are available by default
    return True
