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
import logging.handlers
from pathlib import Path

# Resolve project root: exe parent when frozen (PyInstaller), else script parent.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

LOG_FILE = PROJECT_ROOT / "logs" / "orions-belt.log"
LOG_FILE.parent.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
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
        _migrate_schema(app)
        _seed_builtin_tools(app)
        _ensure_projects_dir(app)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


def _seed_builtin_tools(app):
    """Ensure built-in MCP tools exist in the database with correct schemas.

    Safe to call on every startup — adds missing tools and patches any existing
    tool whose input_schema is still empty (from an older seed run).
    """
    import json
    from app.models.mcp_tool import MCPTool
    from app import db

    builtin = [
        dict(
            name="read_file",
            tier=0,
            description="Read a file from an authorized directory",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to read"},
                    "max_bytes": {"type": "integer", "description": "Maximum bytes to read (default 65536, max 1048576)"},
                },
                "required": ["path"],
            }),
        ),
        dict(
            name="list_directory",
            tier=0,
            description="List files in an authorized directory",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list"},
                },
                "required": ["path"],
            }),
        ),
        dict(
            name="search_files",
            tier=0,
            description="Search for files matching a glob pattern inside an authorized directory",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Root directory to search in"},
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. *.py or *.csv"},
                },
                "required": ["path", "pattern"],
            }),
        ),
        dict(
            name="create_file",
            tier=1,
            description="Create a new file (fails if the file already exists)",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the file to create"},
                    "content": {"type": "string", "description": "Initial file content"},
                },
                "required": ["path"],
            }),
        ),
        dict(
            name="append_to_file",
            tier=1,
            description="Append text to an existing file",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file"},
                    "content": {"type": "string", "description": "Text to append"},
                },
                "required": ["path", "content"],
            }),
        ),
        dict(
            name="call_connector",
            tier=1,
            description="Call a configured data connector (REST API or SQL Server)",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "connector": {"type": "string", "description": "Connector name as configured in Settings"},
                    "action": {"type": "string", "description": "Table name or SELECT query to run"},
                    "params": {"type": "object", "description": "Optional parameters for REST connectors"},
                },
                "required": ["connector", "action"],
            }),
        ),
        dict(
            name="run_sql_query",
            tier=1,
            description="Run a read-only SELECT query via a configured SQL connector",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "connector": {"type": "string", "description": "SQL connector name as configured in Settings"},
                    "query": {"type": "string", "description": "SELECT statement to execute (read-only)"},
                },
                "required": ["connector", "query"],
            }),
        ),
        dict(
            name="search_emails",
            tier=0,
            description="Search Outlook inbox emails by keyword (Windows only)",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to search in subject and body"},
                    "count": {"type": "integer", "description": "Maximum number of emails to return (default 20)"},
                },
                "required": [],
            }),
        ),
        dict(
            name="modify_file",
            tier=2,
            description="Overwrite an existing file with new content",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to overwrite"},
                    "content": {"type": "string", "description": "New file content"},
                },
                "required": ["path", "content"],
            }),
        ),
        dict(
            name="create_directory",
            tier=2,
            description="Create a new directory (and any missing parents)",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to create"},
                },
                "required": ["path"],
            }),
        ),
        dict(
            name="delete_file",
            tier=3,
            description="Delete a file permanently (requires explicit approval)",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to delete"},
                },
                "required": ["path"],
            }),
        ),
        dict(
            name="move_file",
            tier=3,
            description="Move or rename a file (requires explicit approval)",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Absolute path of the file to move"},
                    "destination": {"type": "string", "description": "Absolute destination path"},
                },
                "required": ["source", "destination"],
            }),
        ),
    ]

    for tool_def in builtin:
        existing = MCPTool.query.filter_by(name=tool_def["name"]).first()
        if not existing:
            db.session.add(MCPTool(source="builtin", **tool_def))
        elif not existing.input_schema or existing.input_schema in ("{}", ""):
            # Patch tools created by the old schema-less seeder
            existing.input_schema = tool_def["input_schema"]
            existing.description = tool_def["description"]
    db.session.commit()


def _migrate_schema(app):
    """Add columns introduced after initial release (idempotent)."""
    from app import db
    cols = {
        "sessions": [
            ("archived",     "BOOLEAN NOT NULL DEFAULT 0"),
            ("archived_at",  "DATETIME"),
        ],
        "authorized_directories": [
            ("enabled", "BOOLEAN NOT NULL DEFAULT 1"),
        ],
        "projects": [
            ("folder_path", "VARCHAR(1024)"),
        ],
        "epics": [
            ("plan", "TEXT"),
        ],
        "features": [
            ("plan", "TEXT"),
        ],
        "tasks": [
            ("plan", "TEXT"),
        ],
    }
    with db.engine.connect() as conn:
        for table, additions in cols.items():
            # Fetch existing column names
            existing = {
                row[1]
                for row in conn.execute(db.text(f"PRAGMA table_info({table})"))
            }
            for col_name, col_def in additions:
                if col_name not in existing:
                    conn.execute(db.text(
                        f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"
                    ))
                    print(f"[migrate] added {table}.{col_name}")
        conn.commit()


def _ensure_projects_dir(app):
    """Create the root projects directory and register it as an authorized directory."""
    from config import Config
    from app.models.connector import AuthorizedDirectory
    from app import db

    projects_root = Config.PROJECTS_DIR
    projects_root.mkdir(parents=True, exist_ok=True)
    print(f"[startup] projects root: {projects_root}")

    # Register as an authorized directory so MCP tools can access it
    path_str = str(projects_root)
    existing = AuthorizedDirectory.query.filter_by(path=path_str).first()
    if not existing:
        db.session.add(AuthorizedDirectory(
            path=path_str,
            alias="Projects",
            recursive=True,
            read_only=False,
            max_tier=3,
            enabled=True,
        ))
        db.session.commit()
        print(f"[startup] authorized directory registered: {path_str}")


def _migrate_llm_settings(app):
    """Clean up corrupted LLM provider data from earlier buggy saves."""
    import json
    from app import db
    from app.models.settings import Setting

    # Remove old flat keys that are no longer used
    for key in ("llm.base_url", "llm.api_key", "llm.model", "llm.provider"):
        row = db.session.get(Setting, key)
        if row:
            db.session.delete(row)

    # Fix corrupted llm.providers
    row = db.session.get(Setting, "llm.providers")
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

    # 2. Determine start URL — show first-run page if models not yet downloaded
    from app.routes.first_run import models_ready
    start_url = URL if models_ready(PROJECT_ROOT) else f"{URL}/first-run"
    if start_url != URL:
        log.info("Models not cached — opening first-run setup page")

    # 3. Open native window
    try:
        import webview

        window = webview.create_window(
            title="Orion's Belt",
            url=start_url,
            width=1400,
            height=900,
            min_size=(1024, 600),
            background_color="#0f0f0f",
            maximized=True,
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
