"""Tool result caching — LRU + TTL cache for read-only MCP tools.

Cacheable tools (read_file, list_directory, etc.) get their results stored
so repeated identical calls don't hit the filesystem or database again.
"""
import hashlib
import json
import logging
import time
from collections import OrderedDict

log = logging.getLogger("orions-belt")

# Singleton cache instance
_cache_instance = None


def get_tool_cache() -> "ToolCache":
    """Get or create the tool cache singleton."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = ToolCache()
    return _cache_instance


class ToolCache:
    """LRU + TTL cache for tool execution results."""

    def __init__(self, max_size: int = 256, default_ttl: int = 60):
        self._store: OrderedDict[str, dict] = OrderedDict()
        self._max_size = max_size
        self._default_ttl = default_ttl  # seconds

    def _make_key(self, tool_name: str, args: dict) -> str:
        """Create a stable cache key from tool name + args."""
        args_str = json.dumps(args, sort_keys=True, default=str)
        h = hashlib.sha256(f"{tool_name}:{args_str}".encode()).hexdigest()[:16]
        return f"{tool_name}:{h}"

    def get(self, tool_name: str, args: dict):
        """Get cached result, or None if miss/expired."""
        key = self._make_key(tool_name, args)
        entry = self._store.get(key)
        if entry is None:
            return None

        if time.time() > entry["expires_at"]:
            # Expired — remove and return miss
            del self._store[key]
            return None

        # Move to end (LRU)
        self._store.move_to_end(key)
        return entry["value"]

    def set(self, tool_name: str, args: dict, value, ttl: int = None):
        """Store a result in the cache."""
        key = self._make_key(tool_name, args)

        # Evict oldest if at capacity
        while len(self._store) >= self._max_size:
            self._store.popitem(last=False)

        self._store[key] = {
            "value": value,
            "expires_at": time.time() + (ttl or self._default_ttl),
        }

    def invalidate(self, tool_name: str = None):
        """Clear cache entries. If tool_name given, only clear that tool's entries."""
        if tool_name is None:
            self._store.clear()
        else:
            keys_to_remove = [k for k in self._store if k.startswith(f"{tool_name}:")]
            for k in keys_to_remove:
                del self._store[k]
