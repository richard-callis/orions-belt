"""Plugin API — the interface exposed to plugin register() functions.

Plugins call api.register_tool(...) to add new MCP tools at startup.
"""
import logging

log = logging.getLogger("orions-belt")


class PluginAPI:
    """API surface exposed to plugin register() functions."""

    def __init__(self, manager):
        self._manager = manager

    def register_tool(
        self,
        name: str,
        handler,
        description: str = "",
        input_schema: dict = None,
        tier: int = 0,
    ):
        """Register a new tool from a plugin.

        Args:
            name: Unique tool name (e.g., "my_plugin_search")
            handler: Async callable(tool_name, args) -> str
            description: Human-readable description for LLM
            input_schema: JSON Schema for tool parameters
            tier: Authorization tier (0=read, 1=create, 2=modify, 3=destructive)
        """
        if input_schema is None:
            input_schema = {"type": "object", "properties": {}}

        registration = {
            "name": name,
            "handler": handler,
            "description": description,
            "input_schema": input_schema,
            "tier": tier,
            "plugin": name.split("_")[0] if "_" in name else "unknown",
        }

        self._manager._registered_tools.append(registration)
        log.info("plugin.register_tool name=%s tier=%d", name, tier)
