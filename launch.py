"""
Orion's Belt — Desktop Launcher
Starts Flask in a background thread, opens a native pywebview window.
Sits in the system tray when minimized (like Discord).
"""
import sys
import os
import threading
import time
import logging
from pathlib import Path

# Ensure the project root is on sys.path regardless of where launch.py is called from
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("orions-belt")

PORT = 5000
URL = f"http://localhost:{PORT}"


def run_flask():
    """Start Flask server (non-debug, single-threaded for SQLite safety)."""
    from app import create_app, db
    app = create_app()
    with app.app_context():
        db.create_all()
        _migrate_llm_settings(app)
        _seed_builtin_tools(app)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


def _seed_builtin_tools(app):
    """Ensure built-in MCP tools exist in the database on first run."""
    from app.models.mcp_tool import MCPTool
    from app import db

    builtin = [
        dict(name="read_file",        tier=0, description="Read a file from an authorized directory"),
        dict(name="list_directory",   tier=0, description="List files in an authorized directory"),
        dict(name="search_files",     tier=0, description="Search for files matching a pattern"),
        dict(name="create_file",      tier=1, description="Create a new file (fails if exists)"),
        dict(name="append_to_file",   tier=1, description="Append content to an existing file"),
        dict(name="call_connector",   tier=1, description="Call a configured connector"),
        dict(name="run_sql_query",    tier=1, description="Run a SELECT query via a SQL connector"),
        dict(name="search_emails",    tier=0, description="Search Outlook emails (read-only)"),
        dict(name="modify_file",      tier=2, description="Overwrite an existing file"),
        dict(name="create_directory", tier=2, description="Create a new directory"),
        dict(name="delete_file",      tier=3, description="Delete a file (hard stop — requires approval)"),
        dict(name="move_file",        tier=3, description="Move or rename a file"),
    ]

    for tool_def in builtin:
        existing = MCPTool.query.filter_by(name=tool_def["name"]).first()
        if not existing:
            db.session.add(MCPTool(source="builtin", **tool_def))
    db.session.commit()


def _migrate_llm_settings(app):
    """Clean up corrupted LLM provider data from earlier buggy saves."""
    import json
    from app.models.settings import Setting

    # Remove old flat keys that are no longer used
    for key in ("llm.base_url", "llm.api_key", "llm.model", "llm.provider"):
        row = Setting.query.get(key)
        if row:
            db.session.delete(row)

    # Fix corrupted llm.providers
    row = Setting.query.get("llm.providers")
    if row:
        try:
            providers = json.loads(row.value)
            if not isinstance(providers, list):
                raise ValueError("not a list")
        except (json.JSONDecodeError, ValueError):
            print("[migrate] removing corrupted llm.providers")
            db.session.delete(row)

    db.session.commit()


def wait_for_flask(timeout=10):
    """Poll until Flask is accepting connections."""
    import urllib.request
    import urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{URL}/api/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def create_tray_icon(window):
    """System tray icon with show/quit menu."""
    try:
        import pystray
        from PIL import Image, ImageDraw

        # Draw a simple three-dot icon (Orion's Belt stars)
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        accent = (0, 167, 225)  # #00A7E1
        for x in [10, 28, 46]:
            draw.ellipse([x, 26, x + 10, 36], fill=accent)

        def on_show(icon, item):
            window.show()

        def on_quit(icon, item):
            icon.stop()
            window.destroy()

        menu = pystray.Menu(
            pystray.MenuItem("Open Orion's Belt", on_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )
        return pystray.Icon("orions-belt", img, "Orion's Belt", menu)
    except ImportError:
        log.warning("pystray not installed — system tray disabled")
        return None


def main():
    # 1. Start Flask in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    log.info("Waiting for Flask to start...")
    if not wait_for_flask():
        log.error("Flask did not start in time — check for port conflicts on 5000")
        sys.exit(1)

    log.info(f"Flask ready at {URL}")

    # 2. Open native window
    try:
        import webview

        window = webview.create_window(
            title="Orion's Belt",
            url=URL,
            width=1400,
            height=900,
            min_size=(1024, 600),
            background_color="#0f0f0f",
        )

        # 3. System tray (minimize to tray)
        tray = create_tray_icon(window)

        def on_minimize():
            if tray:
                window.hide()
                if not tray.visible:
                    tray_thread = threading.Thread(target=tray.run, daemon=True)
                    tray_thread.start()

        window.events.minimized += on_minimize

        webview.start(debug=False)

    except ImportError:
        log.warning("pywebview not installed — falling back to browser")
        import webbrowser
        webbrowser.open(URL)
        # Keep Flask alive
        flask_thread.join()


if __name__ == "__main__":
    main()
