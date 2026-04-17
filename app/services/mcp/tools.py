"""
Orion's Belt — MCP Tool Execution Service
Executes tools with tier-based authorization and path safety checks.
"""
import glob as glob_mod
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app import db
from app.models.connector import AuthorizedDirectory
from app.models.mcp_tool import MCPTool
from app.models.logs import AuditLog
from app.models.pii import PIIHashEntry


# ── Tier system ───────────────────────────────────────────────────────────────
# 0 — Auto: read, list, SELECT
# 1 — Auto + Audit: create new file, INSERT
# 2 — Warn: modify existing file, UPDATE (10s countdown in UI)
# 3 — Hard Stop: delete, move, DELETE (requires approval)

TIER_READ = 0
TIER_CREATE = 1
TIER_MODIFY = 2
TIER_DELETE = 3

# System paths that are always blocked
BLOCKED_PATHS = [
    "C:\\Windows",
    "C:\\Windows\\System32",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData\\Microsoft",
    "C:\\bootmgr",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
]


def _is_blocked_path(path: str) -> bool:
    """Check if path is in a blocked system directory."""
    normalized = str(path).replace("/", "\\")
    for blocked in BLOCKED_PATHS:
        if normalized.startswith(blocked):
            return True
    return False


def _authorize_path(path: str) -> bool:
    """Check if path is within an authorized directory."""
    real_path = str(Path(path).resolve())
    authorized = AuthorizedDirectory.query.filter_by(enabled=True).all()
    if not authorized:
        # No directories configured — allow nothing
        return False
    return any(real_path.startswith(dir.path) for dir in authorized)


def _get_effective_tier(path: str, tool_tier: int) -> int:
    """Calculate effective tier based on path settings."""
    real_path = str(Path(path).resolve())
    for dir_entry in AuthorizedDirectory.query.filter_by(enabled=True).all():
        if real_path.startswith(dir_entry.path):
            if dir_entry.read_only:
                return min(tool_tier, TIER_READ)
            if dir_entry.max_tier is not None:
                return min(tool_tier, dir_entry.max_tier)
            break
    return tool_tier


async def execute_tool(tool_name: str, args: dict) -> str:
    """Execute a tool by name with the given args.

    Returns the result as a string. Used by the LLM service's agentic loop.
    """
    tool = MCPTool.query.filter_by(name=tool_name, enabled=True).first()
    if not tool:
        return f"Error: unknown tool '{tool_name}'"

    # Route to the appropriate handler
    handlers = {
        # Tier 0: read operations
        "read_file": _handle_read_file,
        "list_directory": _handle_list_directory,
        "search_files": _handle_search_files,
        "run_sql_query": _handle_run_sql_query,
        "search_emails": _handle_search_emails,
        "call_connector": _handle_call_connector,
        # Tier 1: create operations
        "create_file": _handle_create_file,
        "append_to_file": _handle_append_to_file,
        # Tier 2: modify operations
        "modify_file": _handle_modify_file,
        "create_directory": _handle_create_directory,
        # Tier 3: destructive operations
        "delete_file": _handle_delete_file,
        "move_file": _handle_move_file,
    }

    handler = handlers.get(tool_name)
    if not handler:
        return f"Error: tool '{tool_name}' has no handler"

    try:
        result = await handler(tool_name, args)
        # Log audit
        _log_audit(tool_name, TIER_READ if tool.tier <= TIER_READ else tool.tier, "auto", None, None, result)
        return result
    except Exception as e:
        err_msg = str(e)
        _log_audit(tool_name, tool.tier, "auto", None, None, f"Error: {err_msg}", error=err_msg)
        return f"Error: {err_msg}"


def _log_audit(tool_name: str, tier: int, caller: str, session_id: str | None,
               run_id: str | None, result: str, error: str | None = None):
    """Log an audit entry."""
    log = AuditLog(
        tool_name=tool_name,
        tier=tier,
        caller=caller,
        session_id=session_id,
        run_id=run_id,
        input_summary=result[:500],
        outcome="auto" if tier <= TIER_READ else "pending",
        result_summary=result[:1000],
        error=error,
    )
    db.session.add(log)
    db.session.commit()


# ── Tier 0: Read Operations ──────────────────────────────────────────────────

async def _handle_read_file(tool_name: str, args: dict) -> str:
    """Read a file from an authorized directory."""
    path = args.get("path", "")
    if not path:
        return "Error: path is required"

    # Normalize path
    real_path = str(Path(path).resolve())
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}"

    try:
        content = Path(real_path).read_text(encoding="utf-8")
        max_bytes = args.get("max_bytes", 65536)
        if len(content) > max_bytes:
            content = content[:max_bytes] + "\n[…truncated, file too large]"
        return content
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except PermissionError:
        return f"Error: permission denied: {path}"
    except Exception as e:
        return f"Error reading file: {e}"


async def _handle_list_directory(tool_name: str, args: dict) -> str:
    """List files in an authorized directory."""
    path = args.get("path", ".")
    real_path = str(Path(path).resolve())
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}"

    try:
        entries = sorted(Path(real_path).iterdir())
        lines = []
        for entry in entries:
            suffix = "/" if entry.is_dir() else ""
            size = "" if entry.is_dir() else f" ({entry.stat().st_size:,}B)"
            lines.append(f"  {entry.name}{suffix}{size}")
        return f"Directory: {path}\n" + "\n".join(lines)
    except FileNotFoundError:
        return f"Error: directory not found: {path}"
    except PermissionError:
        return f"Error: permission denied: {path}"


async def _handle_search_files(tool_name: str, args: dict) -> str:
    """Search for files matching a pattern."""
    path = args.get("path", ".")
    pattern = args.get("pattern", "*")
    real_path = str(Path(path).resolve())
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}"

    try:
        matches = list(Path(real_path).glob(f"**/{pattern}"))
        if not matches:
            return f"No files matching '{pattern}' in {path}"
        lines = [str(m) for m in matches[:50]]  # Cap results
        count = len(matches)
        result = "\n".join(lines)
        return f"Found {count} matches (showing first 50):\n{result}"
    except Exception as e:
        return f"Error searching files: {e}"


async def _handle_run_sql_query(tool_name: str, args: dict) -> str:
    """Run a SELECT query via a SQL connector."""
    connector_name = args.get("connector", "")
    query = args.get("query", "")
    if not connector_name or not query:
        return "Error: connector and query are required"

    connector = _get_connector(connector_name)
    if not connector:
        return f"Error: connector '{connector_name}' not found"

    try:
        import pyodbc
        conn = pyodbc.connect(connector["connection_string"])
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchmany(100)  # Cap at 100 rows
        conn.close()

        # Format as table
        header = " | ".join(str(c) for c in columns)
        separator = "-+-".join("-" * len(str(c)) for c in columns)
        data_rows = [" | ".join(str(v) if v is not None else "" for v in row) for row in rows]
        return f"{header}\n{separator}\n" + "\n".join(data_rows)
    except Exception as e:
        return f"Error running query: {e}"


async def _handle_search_emails(tool_name: str, args: dict) -> str:
    """Search Outlook emails (read-only)."""
    import win32com.client  # Windows only
    outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    inbox = outlook.DefaultFolder(6)  # olFolderInbox

    query = args.get("query", "")
    count = args.get("count", 20)

    results = []
    for item in list(inbox.Items)[:count]:
        if not query or query.lower() in (item.Subject or "").lower() or query.lower() in (item.Body or "").lower():
            results.append(f"  {item.Subject} — {item.SenderName} — {item.ReceivedOn.strftime('%Y-%m-%d')}")
    return "\n".join(results) if results else "No matching emails found."


# ── Tier 1: Create Operations ────────────────────────────────────────────────

async def _handle_create_file(tool_name: str, args: dict) -> str:
    """Create a new file (fails if exists)."""
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "Error: path is required"

    real_path = str(Path(path).resolve())
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}"
    if Path(real_path).exists():
        return f"Error: file already exists: {path}"

    try:
        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        Path(real_path).write_text(content or "", encoding="utf-8")
        return f"Created: {path}"
    except Exception as e:
        return f"Error creating file: {e}"


async def _handle_append_to_file(tool_name: str, args: dict) -> str:
    """Append content to an existing file."""
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "Error: path is required"

    real_path = str(Path(path).resolve())
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}"

    try:
        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        Path(real_path).write_text(
            Path(real_path).read_text(encoding="utf-8") + content,
            encoding="utf-8",
        )
        return f"Appended to: {path}"
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except Exception as e:
        return f"Error appending: {e}"


# ── Tier 2: Modify Operations ────────────────────────────────────────────────

async def _handle_modify_file(tool_name: str, args: dict) -> str:
    """Overwrite an existing file."""
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "Error: path is required"

    real_path = str(Path(path).resolve())
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}"

    try:
        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        Path(real_path).write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        return f"Modified: {path} ({lines} lines, {len(content)} chars)"
    except Exception as e:
        return f"Error writing file: {e}"


async def _handle_create_directory(tool_name: str, args: dict) -> str:
    """Create a new directory."""
    path = args.get("path", "")
    if not path:
        return "Error: path is required"

    real_path = str(Path(path).resolve())
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"

    try:
        Path(real_path).mkdir(parents=True, exist_ok=True)
        return f"Created directory: {path}"
    except Exception as e:
        return f"Error creating directory: {e}"


# ── Tier 3: Destructive Operations ───────────────────────────────────────────

async def _handle_delete_file(tool_name: str, args: dict) -> str:
    """Delete a file."""
    path = args.get("path", "")
    if not path:
        return "Error: path is required"

    real_path = str(Path(path).resolve())
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}"

    try:
        Path(real_path).unlink()
        return f"Deleted: {path}"
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except Exception as e:
        return f"Error deleting: {e}"


async def _handle_move_file(tool_name: str, args: dict) -> str:
    """Move or rename a file."""
    src = args.get("source", "")
    dst = args.get("destination", "")
    if not src or not dst:
        return "Error: source and destination are required"

    real_src = str(Path(src).resolve())
    real_dst = str(Path(dst).resolve())
    if _is_blocked_path(real_src) or _is_blocked_path(real_dst):
        return "Error: access denied — system path blocked"

    try:
        Path(real_src).rename(real_dst)
        return f"Moved: {src} → {dst}"
    except FileNotFoundError:
        return f"Error: source not found: {src}"
    except Exception as e:
        return f"Error moving: {e}"


# ── Connector helpers ─────────────────────────────────────────────────────────

def _get_connector(name: str):
    """Get connector config from database."""
    from app.models.connector import Connector
    conn = Connector.query.filter_by(name=name, enabled=True).first()
    if not conn:
        return None
    import json
    return {
        "name": conn.name,
        "type": conn.connector_type,
        "config": json.loads(conn.config or "{}"),
    }


async def _handle_call_connector(tool_name: str, args: dict) -> str:
    """Call a configured connector."""
    connector_name = args.get("connector", "")
    action = args.get("action", "list")
    params = args.get("params", {})

    if not connector_name:
        return "Error: connector name is required"

    connector = _get_connector(connector_name)
    if not connector:
        return f"Error: connector '{connector_name}' not found"

    ctype = connector["type"]
    config = connector["config"]

    try:
        if ctype == "rest_api":
            import httpx
            url = config.get("url", "") + "/" + action
            method = config.get("method", "GET").upper()
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(method, url, json=params)
                return f"HTTP {resp.status_code}\n{resp.text[:2000]}"
        elif ctype == "sql_server":
            import pyodbc
            conn = pyodbc.connect(config.get("connection_string", ""))
            cursor = conn.cursor()
            cursor.execute(action if action.startswith("SELECT") else f"SELECT * FROM {action}")
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchmany(50)
            conn.close()
            header = " | ".join(columns)
            return header + "\n" + "\n".join(" | ".join(str(v) for v in row) for row in rows)
        else:
            return f"Error: unsupported connector type: {ctype}"
    except Exception as e:
        return f"Error calling connector: {e}"


async def _handle_search_emails(tool_name: str, args: dict) -> str:
    """Search Outlook emails."""
    try:
        import win32com.client
    except ImportError:
        return "Error: pywin32 not installed (Windows-only)"

    outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    inbox = outlook.DefaultFolder(6)
    query = args.get("query", "")
    count = args.get("count", 20)

    results = []
    for item in list(inbox.Items)[:count]:
        if not query or query.lower() in (item.Subject or "").lower():
            results.append(f"  {item.Subject} — {item.SenderName} — {item.ReceivedOn.strftime('%Y-%m-%d')}")
    return "\n".join(results) if results else "No matching emails found."
