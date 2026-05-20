"""Plugin discovery — scan extensions directory for loadable plugins."""
import logging
import os
from pathlib import Path

log = logging.getLogger("orions-belt")


def discover_plugins(extensions_dir: str) -> list[Path]:
    """Scan a directory for Python plugin files.

    A valid plugin is a .py file that:
    - Is in the top level of extensions_dir
    - Does not start with underscore
    - Is a regular file (not a directory)

    Args:
        extensions_dir: Path to the extensions directory

    Returns:
        List of Path objects for discovered plugin files
    """
    if not os.path.isdir(extensions_dir):
        log.debug("plugins.discover: extensions_dir=%s does not exist", extensions_dir)
        return []

    plugins = []
    for entry in sorted(os.listdir(extensions_dir)):
        if entry.startswith("_") or not entry.endswith(".py"):
            continue
        full_path = Path(extensions_dir) / entry
        if full_path.is_file():
            plugins.append(full_path)
            log.debug("plugins.discover: found %s", entry)

    log.info("plugins.discover: found %d plugin(s) in %s", len(plugins), extensions_dir)
    return plugins
