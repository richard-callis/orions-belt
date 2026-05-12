"""Plugin system — simplified dynamic tool loading.

Scans `extensions/` directory for Python plugins at startup.
Each plugin exports a `register(api)` function that registers tools, hooks, etc.

Usage:
    from app.services.plugins import plugin_manager
    plugin_manager.load_all()  # Called at app startup
"""
from app.services.plugins.discovery import discover_plugins
from app.services.plugins.api import PluginAPI

# Module-level singleton
_manager = None


def get_plugin_manager():
    """Get or create the plugin manager singleton."""
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager


class PluginManager:
    """Manages plugin discovery, loading, and registration."""

    def __init__(self):
        self._plugins = {}       # name -> plugin_info
        self._registered_tools = []  # List of tool registrations from plugins

    def load_all(self, extensions_dir: str = None) -> list[dict]:
        """Discover and load all plugins from the extensions directory.

        Returns:
            List of plugin info dicts (name, status, error if any)
        """
        import os
        if extensions_dir is None:
            # Default: <project_root>/extensions
            extensions_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                "extensions",
            )

        if not os.path.isdir(extensions_dir):
            return []

        results = []
        for plugin_module in discover_plugins(extensions_dir):
            result = self._load_plugin(plugin_module, extensions_dir)
            results.append(result)

        return results

    def _load_plugin(self, plugin_module, extensions_dir):
        """Load a single plugin module."""
        import os
        import importlib.util
        name = plugin_module.stem  # filename without .py

        # Check for existing plugin
        if name in self._plugins:
            return {"name": name, "status": "skipped", "reason": "already loaded"}

        plugin_path = os.path.join(extensions_dir, f"{plugin_module}.py")

        try:
            # Dynamically import the module
            spec = importlib.util.spec_from_file_location(f"orions_belt.plugin.{name}", plugin_path)
            if spec is None or spec.loader is None:
                return {"name": name, "status": "error", "error": "Could not load module spec"}

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Check for register function
            if not hasattr(module, "register"):
                return {"name": name, "status": "error", "error": "No register() function found"}

            # Create API and register
            api = PluginAPI(self)
            module.register(api)

            # Store plugin info
            self._plugins[name] = {
                "name": name,
                "path": plugin_path,
                "status": "loaded",
                "module": module,
            }

            return {"name": name, "status": "loaded"}

        except Exception as e:
            return {"name": name, "status": "error", "error": str(e)}

    def reload_plugin(self, name: str, extensions_dir: str = None) -> dict:
        """Hot-reload a single plugin."""
        if name not in self._plugins:
            return {"name": name, "status": "error", "error": "Plugin not found"}

        # Unload
        self._plugins.pop(name, None)
        self._registered_tools = [t for t in self._registered_tools if t["plugin"] != name]

        # Reload
        if extensions_dir is None:
            extensions_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                "extensions",
            )
        return self.load_all(extensions_dir)

    @property
    def registered_tools(self):
        """Get all tool registrations from plugins."""
        return self._registered_tools

    @property
    def plugin_names(self):
        """Get list of loaded plugin names."""
        return list(self._plugins.keys())

    def get_plugin(self, name: str):
        """Get plugin info by name."""
        return self._plugins.get(name)

    def get_tool_definitions(self) -> list[dict]:
        """Get plugin tool registrations in OpenAI function-definition format.

        Returns a list of tool definition dicts compatible with
        build_tool_definitions() output format.
        """
        result = []
        for reg in self._registered_tools:
            result.append({
                "type": "function",
                "function": {
                    "name": reg["name"],
                    "description": reg.get("description", ""),
                    "parameters": reg.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return result

    def get_tool_handler(self, tool_name: str):
        """Get the handler for a plugin tool by name, or None."""
        for reg in self._registered_tools:
            if reg["name"] == tool_name:
                return reg["handler"]
        return None


# Import os at module level for the default extensions_dir path
import os
