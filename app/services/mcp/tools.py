"""
Orion's Belt — MCP Tool Execution Service
Executes tools with tier-based authorization and path safety checks.

Mirrors the harness spec with:
- Tool result caching (LRU + TTL for read-only tools)
- Structured error types (for retry logic)
- Tool availability checks (filtered at prompt-build time)
"""
import asyncio
import glob as glob_mod
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from flask import g

from app import db
from app.models.connector import AuthorizedDirectory
from app.models.logs import AuditLog
from app.models.mcp_tool import MCPTool
from app.models.pii import PIIHashEntry
from app.services.audit_chain import compute_row_hash, get_last_row_hash
from app.services.redact import redact_args, redact_text

log = logging.getLogger("orions-belt")


# ── Structured errors (from harness spec) ─────────────────────────────────────

class ToolErrorCategory(str, Enum):
    """Structured error categories for tool execution."""
    NOT_FOUND = "TOOL_NOT_FOUND"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TIMEOUT = "TIMEOUT"
    EXECUTION = "EXECUTION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"


class ToolError(Exception):
    """Structured tool error with category and retry info.

    Tools raise ToolError instead of returning "Error: ...".
    The caller inspects category and retryable to decide retry vs. fail.
    """
    def __init__(self, category: ToolErrorCategory, message: str, retryable: bool = False):
        self.category = category
        self.retryable = retryable
        self.message = message
        super().__init__(message)


# Tier 0 read tools that are cacheable
# run_sql_query is excluded: results are connector-scoped and the cache has no
# authorization dimension, so caching would leak results across agent contexts.
CACHEABLE_TOOLS = {"read_file", "list_directory", "search_files"}
# Tools that mutate the filesystem — after one succeeds, the read caches above
# are stale and must be invalidated so a follow-up read sees fresh content.
WRITE_TOOLS = {"create_file", "append_to_file", "modify_file", "create_directory",
               "delete_file", "move_file", "create_word_document", "create_powerpoint",
               "create_excel", "create_pdf", "create_ado_workitem",
               "run_python", "run_shell", "git_commit"}


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


def _sanitize_path_input(path: str) -> str | None:
    """Return None if path contains dangerous characters, else the path string.

    Security: null bytes can truncate paths at the C layer, allowing attackers
    to bypass extension or suffix checks (e.g. 'safe.txt\\x00.sh').
    """
    if not isinstance(path, str):
        return None
    if "\x00" in path:
        return None
    return path


def _authorize_path(path: str) -> bool:
    """Check if path is within an authorized directory.

    Security hardening applied:
    - os.path.realpath() resolves ALL symlinks (not just the final component).
      This prevents symlink-escape attacks where an authorized dir contains a
      symlink pointing outside the authorized tree.
    - os.sep appended to the prefix prevents /data/safe from matching
      /data/safe-evil (prefix collision attack).
    - The stored dir.path is also realpath'd in case it was set via a symlink.
    """
    real_path = os.path.realpath(path)
    authorized = AuthorizedDirectory.query.filter_by(enabled=True).all()
    if not authorized:
        # No directories configured — allow nothing
        return False
    for dir_entry in authorized:
        auth_real = os.path.realpath(dir_entry.path)
        if real_path == auth_real or real_path.startswith(auth_real + os.sep):
            return True
    return False


def _authorized_dirs_hint() -> str:
    """Short " (authorized: alias=/path, ...)" suffix for not-authorized errors,
    so an agent that guessed a relative/wrong path can self-correct on retry
    instead of guessing blindly again. Safe to expose — these are directories
    the user themselves explicitly authorized via Settings, not a leak."""
    dirs = AuthorizedDirectory.query.filter_by(enabled=True).all()
    if not dirs:
        return " (no authorized directories are configured)"
    listing = ", ".join(f"{d.alias}={d.path}" for d in dirs)
    return f" (authorized directories: {listing})"


def _get_effective_tier(path: str, tool_tier: int) -> int:
    """Calculate effective tier based on path settings."""
    real_path = os.path.realpath(path)
    for dir_entry in AuthorizedDirectory.query.filter_by(enabled=True).all():
        auth_real = os.path.realpath(dir_entry.path)
        # SECURITY: use realpath + os.sep to prevent prefix-collision attacks
        if real_path == auth_real or real_path.startswith(auth_real + os.sep):
            if dir_entry.read_only:
                return min(tool_tier, TIER_READ)
            if dir_entry.max_tier is not None:
                return min(tool_tier, dir_entry.max_tier)
            break
    return tool_tier


def run_tool_sync(tool_name: str, args: dict, session_id: str | None = None, run_id: str | None = None) -> str:
    """Run the async execute_tool() from a synchronous context.

    The single entry point for "run an MCP tool" from sync callers (chat
    streaming generators, AgentRuntime, tool-approval resolution). Lives here
    rather than in a route module so services (AgentRuntime) don't have to
    import a Flask route to execute a tool.
    """
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            execute_tool(tool_name, args, session_id=session_id, run_id=run_id)
        )
    finally:
        loop.close()


async def execute_tool(tool_name: str, args: dict, *, session_id: str | None = None, run_id: str | None = None) -> str:
    """Execute a tool by name with the given args.

    Args:
        tool_name: Name of the tool to execute.
        args: Tool arguments dict.
        session_id: Optional session ID for audit trail attribution.
        run_id: Optional run ID for audit trail attribution.

    Returns the result as a string. Used by the LLM service's agentic loop.

    Integrates:
    - Tool result caching (cacheable read-only tools skip execution on hit)
    - Structured error handling (ToolError exceptions propagate)
    """
    # Set session_id/run_id on g context for _log_audit to read
    if session_id:
        g.orions_belt_session_id = session_id
    if run_id:
        g.orions_belt_run_id = run_id

    # Check tool exists
    tool = MCPTool.query.filter_by(name=tool_name, enabled=True).first()
    if not tool:
        log.warning("mcp.execute: unknown or disabled tool=%r", tool_name)
        return f"Error: unknown tool '{tool_name}'"

    # Check cache for cacheable tools
    if tool_name in CACHEABLE_TOOLS:
        from app.services.mcp.cache import get_tool_cache
        cached = get_tool_cache().get(tool_name, args)
        if cached is not None:
            log.info("mcp.cache HIT tool=%s", tool_name)
            return cached if isinstance(cached, str) else str(cached)

    # Enforce directory-level tier caps (read_only, max_tier) for path-based tools.
    # _get_effective_tier returns min(tool.tier, directory.max_tier); if that is
    # lower than the tool's natural tier the directory does not allow this operation.
    # Check EVERY path argument — move_file has both source and destination, and
    # writing into a read_only directory must be blocked regardless of which arg
    # it arrives in.
    path_args = [args.get(k) for k in ("path", "src", "dest", "source", "destination", "working_dir") if args.get(k)]
    for path_arg in path_args:
        effective_tier = _get_effective_tier(str(path_arg), tool.tier)
        if effective_tier < tool.tier:
            tier_names = {TIER_READ: "read-only (Tier 0)", TIER_CREATE: "create (Tier 1)",
                          TIER_MODIFY: "modify (Tier 2)", TIER_DELETE: "delete (Tier 3)"}
            log.warning("mcp.blocked  tool=%s path=%r dir_max_tier=%d tool_tier=%d",
                        tool_name, path_arg, effective_tier, tool.tier)
            return (f"Error: '{path_arg}' is in a directory that only allows "
                    f"{tier_names.get(effective_tier, f'tier {effective_tier}')} operations")

    # Route to the appropriate handler
    handlers = {
        # Tier 0: read operations
        "read_file": _handle_read_file,
        "list_directory": _handle_list_directory,
        "search_files": _handle_search_files,
        "run_sql_query": _handle_run_sql_query,
        "search_emails": _handle_search_emails,
        "call_connector": _handle_call_connector,
        "fetch_url": _handle_fetch_url,
        "get_github_pr_status": _handle_get_github_pr_status,
        "git_status": _handle_git_status,
        "git_diff": _handle_git_diff,
        "git_log": _handle_git_log,
        "search_jira_issues": _handle_search_jira_issues,
        "search_linear_issues": _handle_search_linear_issues,
        "check_calendar_availability": _handle_check_calendar_availability,
        "read_onedrive_file": _handle_read_onedrive_file,
        "query_salesforce": _handle_query_salesforce,
        "search_documents": _handle_search_documents,
        # Tier 1: create operations
        "create_file": _handle_create_file,
        "append_to_file": _handle_append_to_file,
        "create_word_document": _handle_create_word_document,
        "create_powerpoint": _handle_create_powerpoint,
        "create_excel": _handle_create_excel,
        "create_pdf": _handle_create_pdf,
        "create_ado_workitem": _handle_create_ado_workitem,
        "create_github_issue": _handle_create_github_issue,
        "create_github_pr": _handle_create_github_pr,
        "comment_on_github_pr": _handle_comment_on_github_pr,
        "create_jira_issue": _handle_create_jira_issue,
        "create_linear_issue": _handle_create_linear_issue,
        "create_google_task": _handle_create_google_task,
        "create_onedrive_file": _handle_create_onedrive_file,
        "post_teams_message": _handle_post_teams_message,
        "create_planner_task": _handle_create_planner_task,
        "create_salesforce_record": _handle_create_salesforce_record,
        # Tier 2: modify operations
        "modify_file": _handle_modify_file,
        "create_directory": _handle_create_directory,
        "send_email": _handle_send_email,
        "http_request": _handle_http_request,
        "git_commit": _handle_git_commit,
        "create_calendar_event": _handle_create_calendar_event,
        # Tier 3: destructive operations
        "delete_file": _handle_delete_file,
        "move_file": _handle_move_file,
        "run_python": _handle_run_python,
        "run_shell": _handle_run_shell,
    }

    handler = handlers.get(tool_name)
    if not handler:
        # Fall back to plugin-registered handlers
        try:
            from app.services.plugins import get_plugin_manager
            plugin_handler = get_plugin_manager().get_tool_handler(tool_name)
            if plugin_handler:
                handler = plugin_handler
            else:
                log.warning("mcp.execute: unknown or disabled tool=%r", tool_name)
                return f"Error: unknown tool '{tool_name}'"
        except Exception:
            log.warning("mcp.execute: unknown or disabled tool=%r", tool_name)
            return f"Error: unknown tool '{tool_name}'"

    # Sanitise args for logging — redact known secret shapes/field names,
    # then truncate large values. Redaction runs first: truncating before
    # redacting could leave a partial-but-still-recognizable secret prefix
    # in the log instead of removing it.
    input_params = json.dumps(
        {k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v)
         for k, v in redact_args(args).items()},
        default=str,
    )
    log.info("mcp.call  tool=%s tier=%d args=%s", tool_name, tool.tier, input_params)
    t0 = time.time()

    try:
        result = await handler(tool_name, args)
        elapsed_ms = int((time.time() - t0) * 1000)

        # Cache successful results for cacheable tools
        if tool_name in CACHEABLE_TOOLS and not (isinstance(result, str) and result.startswith("Error:")):
            from app.services.mcp.cache import get_tool_cache
            get_tool_cache().set(tool_name, args, result)

        # Invalidate stale read caches after a successful write.
        if tool_name in WRITE_TOOLS and not (isinstance(result, str) and result.startswith("Error:")):
            from app.services.mcp.cache import get_tool_cache
            cache = get_tool_cache()
            for read_tool in CACHEABLE_TOOLS:
                cache.invalidate(read_tool)

        is_error = isinstance(result, str) and result.startswith("Error:")
        if is_error:
            log.warning("mcp.result tool=%s elapsed=%dms result=%s",
                        tool_name, elapsed_ms, redact_text(result)[:300])
        else:
            # Tool results (read_file, fetch_url, search_documents, run_shell, ...)
            # can contain the literal contents of whatever was read/fetched —
            # redact known secret shapes before truncating, same ordering
            # reasoning as the args log above.
            result_text = result if isinstance(result, str) else str(result)
            preview = redact_text(result_text)[:200].replace("\n", "\\n")
            log.info("mcp.result tool=%s elapsed=%dms preview=%s", tool_name, elapsed_ms, preview)
        _log_audit(
            tool_name,
            TIER_READ if tool.tier <= TIER_READ else tool.tier,
            getattr(g, "current_user", None) or "",
            getattr(g, "orions_belt_session_id", None),
            getattr(g, "orions_belt_run_id", None),
            input_params,
            result,
        )
        return result
    except ToolError as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        log.warning("mcp.error  tool=%s elapsed=%dms category=%s %s",
                     tool_name, elapsed_ms, e.category, redact_text(e.message))
        _log_audit(
            tool_name, tool.tier, getattr(g, "current_user", None) or "",
            getattr(g, "orions_belt_session_id", None),
            getattr(g, "orions_belt_run_id", None),
            input_params,
            e.message, error=e.message,
        )
        return f"Error: {e.message}"
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        err_msg = str(e)
        log.error("mcp.error  tool=%s elapsed=%dms error=%s", tool_name, elapsed_ms,
                   redact_text(err_msg), exc_info=True)
        _log_audit(
            tool_name, tool.tier, getattr(g, "current_user", None) or "",
            getattr(g, "orions_belt_session_id", None),
            getattr(g, "orions_belt_run_id", None),
            input_params,
            f"Error: {err_msg}", error=err_msg,
        )
        return f"Error: {err_msg}"


def _log_audit(tool_name: str, tier: int, caller: str, session_id: str | None,
               run_id: str | None, input_params: str, result: str, error: str | None = None):
    """Log an audit entry.

    Args:
        tool_name: Name of the tool executed.
        tier: Security tier of the tool.
        caller: Username of the caller.
        session_id: Session ID if applicable.
        run_id: Agent run ID if applicable.
        input_params: Sanitised tool input parameters (truncated, no PII).
        result: Tool output/result string.
        error: Optional error message.

    input_params/result/error are redacted here (not just at the call
    sites) because AuditLog rows are persisted indefinitely and queryable
    via the in-app Logs viewer — a more exposed, longer-lived sink than a
    rotating log file. Redacting here covers all three call sites (success,
    ToolError, and the catch-all Exception handler) uniformly; re-redacting
    an already-redacted input_params (the success path pre-redacts it for
    its own log.info call) is a harmless no-op since "[REDACTED:...]"
    never matches a secret pattern.
    """
    input_params = redact_text(input_params)
    result = redact_text(result)
    error = redact_text(error)
    # Outcome reflects what actually happened (this runs AFTER execution):
    #   error     → the tool raised/returned an error (not a policy rejection)
    #   approved  → a Tier-3 tool ran, which only happens after explicit approval
    #   auto      → Tier 0-2 ran automatically (audited; Tier 2 is warn-level)
    if error:
        outcome = "error"
    elif tier >= TIER_DELETE:
        outcome = "approved"
    else:
        outcome = "auto"

    created_at = datetime.now(timezone.utc)
    input_summary = input_params[:500]
    result_summary = result[:1000]
    # Hash over exactly what's persisted below (post-truncation) — verify_chain()
    # recomputes from the stored row, so hashing the pre-truncation values here
    # would make every row fail verification against its own content.
    previous_hash = get_last_row_hash()
    row_hash = compute_row_hash(
        previous_hash, created_at, tool_name, tier, caller, session_id, run_id,
        input_summary, outcome, result_summary, error,
    )

    log = AuditLog(
        created_at=created_at,
        tool_name=tool_name,
        tier=tier,
        caller=caller,
        session_id=session_id,
        run_id=run_id,
        input_summary=input_summary,
        outcome=outcome,
        result_summary=result_summary,
        error=error,
        previous_hash=previous_hash,
        row_hash=row_hash,
    )
    db.session.add(log)
    db.session.commit()


# ── Tier 0: Read Operations ──────────────────────────────────────────────────

MAX_READ_BYTES = 1 * 1024 * 1024   # 1 MB hard cap on file reads
MAX_TOOL_TURNS = 20                # hard cap on per-session tool loop turns


async def _handle_read_file(tool_name: str, args: dict) -> str:
    """Read a file from an authorized directory."""
    path = _sanitize_path_input(args.get("path", ""))
    if not path:
        return "Error: path is required (or contains invalid characters)"

    # Normalize path
    real_path = os.path.realpath(path)
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"

    try:
        content = Path(real_path).read_text(encoding="utf-8")
        # SECURITY: cap file read size to prevent memory exhaustion
        max_bytes = min(int(args.get("max_bytes", 65536)), MAX_READ_BYTES)
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
    path = _sanitize_path_input(args.get("path", ".")) or "."
    real_path = os.path.realpath(path)
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"

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
    path = _sanitize_path_input(args.get("path", ".")) or "."
    pattern = args.get("pattern", "*")
    real_path = os.path.realpath(path)
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"

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


def _assert_select_only(query: str) -> str | None:
    """Return None if query is safe (SELECT-only), or an error string.

    SECURITY: Strips SQL comments before checking to prevent bypass patterns
    like /* DROP */ SELECT. Only SELECT statements are permitted.
    """
    stripped = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
    stripped = re.sub(r"--[^\n]*", "", stripped)
    stripped = stripped.strip().rstrip(";")
    # Block stacked queries — semicolons after stripping comments indicate a second statement
    if ";" in stripped:
        return "Error: multi-statement queries are not permitted"
    if not re.match(r"^SELECT\b", stripped, re.IGNORECASE):
        return "Error: only SELECT queries are permitted via MCP tools"
    return None


async def _handle_search_documents(tool_name: str, args: dict) -> str:
    """Semantic search over locally indexed documents (see
    app/services/doc_index.py — POST /mcp/api/directories/<id>/reindex to
    build the index). Tier 0, read-only.

    Deliberately NOT auto-injected into any prompt — called on demand, like
    any other tool, never spliced into a system prompt the way recalled
    Memory is. Results are PII-scanned (fails open, same as every other
    PII-scan call site in this app) and wrapped in explicit delimiters
    marking them as untrusted document content, not instructions — a
    returned snippet is attacker-influenceable text arriving through the
    same untrusted tool-result channel as a file read or a web fetch.
    """
    query = (args.get("query") or "").strip()
    try:
        top_k = max(1, min(int(args.get("top_k") or 5), 20))
    except (TypeError, ValueError):
        top_k = 5

    if not query:
        return "Error: query is required"

    from app.services.doc_index import search_documents

    try:
        hits = search_documents(query, top_k=top_k)
    except Exception as e:
        return f"Error searching documents: {e}"

    if not hits:
        return "No matching documents found (or nothing has been indexed yet)"

    nonce = uuid.uuid4().hex[:8]
    lines = [f"<<<UNTRUSTED-DOCUMENT-CONTENT-{nonce}>>>"]
    for hit in hits:
        snippet = hit["content"]
        try:
            from app.services.pii_guard import get_pii_guard
            snippet, _detected, _types = get_pii_guard().scan(snippet, direction="outbound")
        except Exception as e:
            log.warning("search_documents: PII scan failed: %s — returning unscanned", e)
        lines.append(f"\n[{hit['file_path']} — chunk {hit['chunk_index']}, score={hit['score']:.3f}]\n{snippet}")
    lines.append(f"<<<END-UNTRUSTED-DOCUMENT-CONTENT-{nonce}>>>")
    return "\n".join(lines)


async def _handle_run_sql_query(tool_name: str, args: dict) -> str:
    """Run a SELECT query via a SQL connector."""
    connector_name = args.get("connector", "")
    query = args.get("query", "")
    if not connector_name or not query:
        return "Error: connector and query are required"

    # SECURITY: enforce read-only access — prevent DROP, INSERT, UPDATE, etc.
    err = _assert_select_only(query)
    if err:
        return err

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


# Response body cap for fetch_url/http_request — these hit arbitrary,
# LLM-chosen hosts (not a connector's own configured base_url), so nothing
# bounds response size upstream; httpx will otherwise buffer the full body
# regardless of how large it is. Streamed and enforced during download, not
# after, so a multi-GB response can't be fully pulled first and discarded.
_MAX_HTTP_RESPONSE_BYTES = 500_000


async def _http_fetch_capped(client, method: str, url: str, **kwargs) -> tuple:
    """GET/POST/etc via a streaming request, capped at _MAX_HTTP_RESPONSE_BYTES.
    Returns (status_code, headers, text, truncated)."""
    async with client.stream(method, url, **kwargs) as resp:
        chunks = []
        total = 0
        truncated = False
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > _MAX_HTTP_RESPONSE_BYTES:
                chunks.append(chunk[: _MAX_HTTP_RESPONSE_BYTES - (total - len(chunk))])
                truncated = True
                break
            chunks.append(chunk)
        content = b"".join(chunks)
        text = content.decode(resp.encoding or "utf-8", errors="replace")
        return resp.status_code, resp.headers, text, truncated


async def _handle_fetch_url(tool_name: str, args: dict) -> str:
    """Fetch an HTTP/HTTPS URL via GET and return its response body as text."""
    from app.services.connector_auth import validate_untrusted_url

    url = (args.get("url") or "").strip()
    if not url:
        return "Error: url is required"
    err = validate_untrusted_url(url)
    if err:
        return err

    try:
        timeout = min(float(args.get("timeout") or 10), 30)
    except (TypeError, ValueError):
        timeout = 10

    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            hops = 0
            while True:
                status, headers, text, truncated = await _http_fetch_capped(client, "GET", url)
                if status not in (301, 302, 303, 307, 308):
                    break
                location = headers.get("location")
                if not location or hops >= 3:
                    break
                # Revalidate the redirect TARGET before following it — a URL
                # that passed the initial check can still redirect to a
                # private/loopback address, which is exactly what
                # follow_redirects=False + manual revalidation exists to catch.
                from urllib.parse import urljoin
                next_url = urljoin(url, location)
                redirect_err = validate_untrusted_url(next_url)
                if redirect_err:
                    return f"Error: redirect target blocked — {redirect_err}"
                url = next_url
                hops += 1
        if status >= 400:
            return f"Error: HTTP {status} fetching {url}"
        if truncated:
            text += "\n...[truncated]"
        return text
    except httpx.TimeoutException:
        return f"Error: request to {url} timed out after {timeout}s"
    except Exception as e:
        return f"Error fetching {url}: {e}"



# ── Tier 1: Create Operations ────────────────────────────────────────────────

async def _handle_create_file(tool_name: str, args: dict) -> str:
    """Create a new file (fails if exists)."""
    path = _sanitize_path_input(args.get("path", ""))
    content = args.get("content", "")
    if not path:
        return "Error: path is required (or contains invalid characters)"

    real_path = os.path.realpath(path)
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"
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
    path = _sanitize_path_input(args.get("path", ""))
    content = args.get("content", "")
    if not path:
        return "Error: path is required (or contains invalid characters)"

    real_path = os.path.realpath(path)
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"

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


# ── Tier 1: Office Document Generation ───────────────────────────────────────
# Agent-authored deliverables: Word/PowerPoint/Excel/PDF. Same tier and path
# authorization as create_file — these only WRITE a new file, so a plain
# "create" tier (not modify/destructive) applies. All libs are pure Python
# (python-docx, python-pptx, openpyxl, reportlab) — no system deps.

MAX_DOC_BLOCKS = 500     # cap on paragraphs/slides/rows to bound generation time
MAX_DOC_CONTENT_CHARS = 200_000


def _authorize_new_file(path_arg: str) -> tuple[str | None, str | None]:
    """Shared create-file preamble: sanitize, block, authorize, and refuse if
    the file already exists. Returns (real_path, None) on success or
    (None, error_message) on failure."""
    path = _sanitize_path_input(path_arg or "")
    if not path:
        return None, "Error: path is required (or contains invalid characters)"
    real_path = os.path.realpath(path)
    if _is_blocked_path(real_path):
        return None, f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return None, f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"
    if Path(real_path).exists():
        return None, f"Error: file already exists: {path}"
    return real_path, None


def _split_paragraphs(content: str) -> list[str]:
    """Split freeform text into paragraphs on blank lines, capped in count."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", content or "") if p.strip()]
    return parts[:MAX_DOC_BLOCKS]


async def _handle_create_word_document(tool_name: str, args: dict) -> str:
    """Create a .docx file from plain text.

    Args: path (required), title (optional, becomes the document heading),
    content (required). Paragraphs are separated by a blank line; a paragraph
    starting with "# "/"## "/"### " becomes a heading; lines starting with
    "- " or "* " become a bullet list.
    """
    real_path, err = _authorize_new_file(args.get("path", ""))
    if err:
        return err

    content = str(args.get("content", ""))[:MAX_DOC_CONTENT_CHARS]
    if not content.strip() and not args.get("title"):
        return "Error: content or title is required"

    try:
        import docx
    except ImportError:
        return "Error: python-docx not installed — run: pip install python-docx"

    try:
        doc = docx.Document()
        if args.get("title"):
            doc.add_heading(str(args["title"])[:300], level=0)

        for para in _split_paragraphs(content):
            heading_m = re.match(r"^(#{1,3})\s+(.*)", para)
            if heading_m:
                doc.add_heading(heading_m.group(2).strip(), level=len(heading_m.group(1)))
                continue
            lines = para.split("\n")
            if all(re.match(r"^[-*]\s+", l) for l in lines):
                for l in lines:
                    doc.add_paragraph(re.sub(r"^[-*]\s+", "", l).strip(), style="List Bullet")
            else:
                doc.add_paragraph(para)

        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        doc.save(real_path)
        return f"Created Word document: {args.get('path')}"
    except Exception as e:
        return f"Error creating Word document: {e}"


async def _handle_create_powerpoint(tool_name: str, args: dict) -> str:
    """Create a .pptx file.

    Args: path (required), title (optional, adds a title slide),
    slides (required) — a list of {"title": str, "bullets": [str, ...]}.
    """
    real_path, err = _authorize_new_file(args.get("path", ""))
    if err:
        return err

    slides = args.get("slides")
    if not isinstance(slides, list) or not slides:
        return "Error: slides (a non-empty list of {title, bullets}) is required"
    slides = slides[:MAX_DOC_BLOCKS]

    try:
        from pptx import Presentation
        from pptx.util import Pt
    except ImportError:
        return "Error: python-pptx not installed — run: pip install python-pptx"

    try:
        prs = Presentation()

        if args.get("title"):
            title_layout = prs.slide_layouts[0]
            slide = prs.slides.add_slide(title_layout)
            slide.shapes.title.text = str(args["title"])[:300]
            if args.get("subtitle") and len(slide.placeholders) > 1:
                slide.placeholders[1].text = str(args["subtitle"])[:300]

        bullet_layout = prs.slide_layouts[1]
        for s in slides:
            if not isinstance(s, dict):
                continue
            slide = prs.slides.add_slide(bullet_layout)
            slide.shapes.title.text = str(s.get("title", ""))[:300]
            body = slide.placeholders[1].text_frame
            bullets = s.get("bullets") or []
            if isinstance(bullets, str):
                bullets = [bullets]
            body.clear()
            for i, b in enumerate(bullets[:50]):
                p = body.paragraphs[0] if i == 0 else body.add_paragraph()
                p.text = str(b)[:500]
                p.font.size = Pt(18)

        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        prs.save(real_path)
        return f"Created PowerPoint: {args.get('path')} ({len(slides)} slide(s))"
    except Exception as e:
        return f"Error creating PowerPoint: {e}"


async def _handle_create_excel(tool_name: str, args: dict) -> str:
    """Create an .xlsx file.

    Args: path (required), sheets (required) — either a single sheet's data
    as {"headers": [...], "rows": [[...], ...]}, or multiple sheets as
    {"Sheet1": {"headers": [...], "rows": [...]}, "Sheet2": {...}}.
    """
    real_path, err = _authorize_new_file(args.get("path", ""))
    if err:
        return err

    sheets = args.get("sheets")
    if not isinstance(sheets, dict) or not sheets:
        return "Error: sheets is required (see tool description for shape)"

    # Normalize: a single {"headers":.., "rows":..} dict means one sheet.
    if "rows" in sheets or "headers" in sheets:
        sheets = {args.get("sheet_name", "Sheet1"): sheets}

    try:
        import openpyxl
    except ImportError:
        return "Error: openpyxl not installed — run: pip install openpyxl"

    try:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)  # drop the default blank sheet

        sheet_names = list(sheets.items())[:50]
        for name, data in sheet_names:
            if not isinstance(data, dict):
                continue
            ws = wb.create_sheet(title=str(name)[:31])  # Excel sheet-name limit
            headers = data.get("headers") or []
            rows = data.get("rows") or []
            if headers:
                ws.append([str(h)[:200] for h in headers])
            for row in rows[:MAX_DOC_BLOCKS * 20]:
                if isinstance(row, (list, tuple)):
                    ws.append([("" if c is None else c) for c in row])

        if not wb.sheetnames:
            return "Error: no valid sheet data provided"

        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        wb.save(real_path)
        return f"Created Excel workbook: {args.get('path')} ({len(wb.sheetnames)} sheet(s))"
    except Exception as e:
        return f"Error creating Excel workbook: {e}"


async def _handle_create_pdf(tool_name: str, args: dict) -> str:
    """Create a .pdf file from plain text.

    Args: path (required), title (optional), content (required). Paragraphs
    are separated by a blank line, mirroring create_word_document.
    """
    real_path, err = _authorize_new_file(args.get("path", ""))
    if err:
        return err

    content = str(args.get("content", ""))[:MAX_DOC_CONTENT_CHARS]
    if not content.strip() and not args.get("title"):
        return "Error: content or title is required"

    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
        from reportlab.lib.units import inch
        from xml.sax.saxutils import escape as xml_escape
    except ImportError:
        return "Error: reportlab not installed — run: pip install reportlab"

    try:
        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        styles = getSampleStyleSheet()
        story = []

        if args.get("title"):
            story.append(Paragraph(xml_escape(str(args["title"])[:300]), styles["Title"]))
            story.append(Spacer(1, 0.25 * inch))

        for para in _split_paragraphs(content):
            heading_m = re.match(r"^(#{1,3})\s+(.*)", para)
            if heading_m:
                level = len(heading_m.group(1))
                style = styles["Heading1"] if level == 1 else styles["Heading2"] if level == 2 else styles["Heading3"]
                story.append(Paragraph(xml_escape(heading_m.group(2).strip()), style))
                continue
            lines = para.split("\n")
            if all(re.match(r"^[-*]\s+", l) for l in lines):
                items = [ListItem(Paragraph(xml_escape(re.sub(r"^[-*]\s+", "", l).strip()), styles["Normal"]))
                         for l in lines]
                story.append(ListFlowable(items, bulletType="bullet"))
            else:
                story.append(Paragraph(xml_escape(para).replace("\n", "<br/>"), styles["Normal"]))
            story.append(Spacer(1, 0.15 * inch))

        doc = SimpleDocTemplate(real_path, pagesize=LETTER)
        doc.build(story)
        return f"Created PDF: {args.get('path')}"
    except Exception as e:
        return f"Error creating PDF: {e}"


# ── Tier 2: Modify Operations ────────────────────────────────────────────────

async def _handle_modify_file(tool_name: str, args: dict) -> str:
    """Overwrite an existing file."""
    path = _sanitize_path_input(args.get("path", ""))
    content = args.get("content", "")
    if not path:
        return "Error: path is required (or contains invalid characters)"

    real_path = os.path.realpath(path)
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"

    try:
        Path(real_path).parent.mkdir(parents=True, exist_ok=True)
        Path(real_path).write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        return f"Modified: {path} ({lines} lines, {len(content)} chars)"
    except Exception as e:
        return f"Error writing file: {e}"


async def _handle_create_directory(tool_name: str, args: dict) -> str:
    """Create a new directory."""
    path = _sanitize_path_input(args.get("path", ""))
    if not path:
        return "Error: path is required (or contains invalid characters)"

    real_path = os.path.realpath(path)
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    # SECURITY FIX: authorization check was missing — added to prevent
    # arbitrary directory creation outside of authorized paths.
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"

    try:
        Path(real_path).mkdir(parents=True, exist_ok=True)
        return f"Created directory: {path}"
    except Exception as e:
        return f"Error creating directory: {e}"


# ── Tier 3: Destructive Operations ───────────────────────────────────────────

async def _handle_delete_file(tool_name: str, args: dict) -> str:
    """Delete a file."""
    path = _sanitize_path_input(args.get("path", ""))
    if not path:
        return "Error: path is required (or contains invalid characters)"

    real_path = os.path.realpath(path)
    if _is_blocked_path(real_path):
        return f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"

    try:
        Path(real_path).unlink()
        return f"Deleted: {path}"
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except Exception as e:
        return f"Error deleting: {e}"


async def _handle_move_file(tool_name: str, args: dict) -> str:
    """Move or rename a file."""
    src = _sanitize_path_input(args.get("source", ""))
    dst = _sanitize_path_input(args.get("destination", ""))
    if not src or not dst:
        return "Error: source and destination are required (or contain invalid characters)"

    real_src = os.path.realpath(src)
    real_dst = os.path.realpath(dst)
    if _is_blocked_path(real_src) or _is_blocked_path(real_dst):
        return "Error: access denied — system path blocked"
    # SECURITY FIX: auth checks were missing on both source and destination.
    if not _authorize_path(real_src):
        return f"Error: source directory not authorized: {src}{_authorized_dirs_hint()}"
    if not _authorize_path(real_dst):
        return f"Error: destination directory not authorized: {dst}{_authorized_dirs_hint()}"

    try:
        Path(real_src).rename(real_dst)
        return f"Moved: {src} → {dst}"
    except FileNotFoundError:
        return f"Error: source not found: {src}"
    except Exception as e:
        return f"Error moving: {e}"


def _real_python_executable() -> str | None:
    """Resolve an actual Python interpreter to run code with.

    sys.executable is NOT a Python interpreter in the PyInstaller-frozen
    build this app ships as (getattr(sys, "frozen", False)) — it's
    OrionsBelt.exe itself, so subprocess.run([sys.executable, "-c", code])
    would relaunch the whole app instead of running the snippet. Falls back
    to whatever "python"/"python3" is on PATH; returns None if neither
    exists so the caller can fail with a clear error instead of silently
    doing the wrong thing.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python3") or shutil.which("python")


_MAX_SUBPROCESS_OUTPUT_CHARS = 20_000


async def _handle_run_python(tool_name: str, args: dict) -> str:
    """Execute a Python code snippet in a subprocess and return stdout/stderr.

    Not sandboxed (no container/seccomp) — this app's threat model gates
    dangerous operations by tier, not OS-level isolation (run_shell, the
    equivalent tool for arbitrary commands, is Tier 3 for the same reason).
    Takes no path argument, so it never goes through _authorize_path — that
    is exactly why it's Tier 3 rather than Tier 2: it has no filesystem
    authorization boundary the way file tools do.
    """
    code = args.get("code") or ""
    if not code:
        return "Error: code is required"
    try:
        timeout = min(float(args.get("timeout") or 30), 120)
    except (TypeError, ValueError):
        timeout = 30

    python = _real_python_executable()
    if not python:
        return "Error: no Python interpreter found to run code with"

    try:
        proc = await asyncio.create_subprocess_exec(
            python, "-c", code,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Error: run_python timed out after {timeout}s"
        out = stdout.decode("utf-8", errors="replace")[:_MAX_SUBPROCESS_OUTPUT_CHARS]
        err = stderr.decode("utf-8", errors="replace")[:_MAX_SUBPROCESS_OUTPUT_CHARS]
        return f"exit_code={proc.returncode}\nstdout:\n{out}\nstderr:\n{err}"
    except Exception as e:
        return f"Error running Python code: {e}"


async def _handle_run_shell(tool_name: str, args: dict) -> str:
    """Execute a shell command and return stdout/stderr. Tier 3 — requires
    explicit approval, same as delete_file/move_file."""
    command = args.get("command") or ""
    if not command:
        return "Error: command is required"
    try:
        timeout = min(float(args.get("timeout") or 60), 300)
    except (TypeError, ValueError):
        timeout = 60

    working_dir = _sanitize_path_input(args.get("working_dir") or "")
    if working_dir:
        real_wd = os.path.realpath(working_dir)
        if _is_blocked_path(real_wd):
            return f"Error: access denied — system path blocked: {working_dir}"
        if not _authorize_path(real_wd):
            return f"Error: working_dir not authorized: {working_dir}{_authorized_dirs_hint()}"
    else:
        authorized = AuthorizedDirectory.query.filter_by(enabled=True).first()
        if not authorized:
            return "Error: no authorized directories configured — set working_dir explicitly or configure one in Settings"
        real_wd = os.path.realpath(authorized.path)
        # execute_tool's path_args tier-cap check only inspects args the
        # caller actually supplied — an omitted working_dir contributes
        # nothing to that check, so this fallback directory's own
        # read_only/max_tier cap must be enforced here explicitly. Without
        # this, a directory capped below Tier 3 (e.g. read-only) could be
        # bypassed simply by not passing working_dir at all.
        tool_row = MCPTool.query.filter_by(name="run_shell", enabled=True).first()
        tool_tier = tool_row.tier if tool_row else TIER_DELETE
        effective_tier = _get_effective_tier(real_wd, tool_tier)
        if effective_tier < tool_tier:
            return (f"Error: the default directory ({authorized.path}) only allows "
                    f"tier {effective_tier} operations — set working_dir explicitly to "
                    f"an authorized directory that permits this")

    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=real_wd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Error: run_shell timed out after {timeout}s"
        out = stdout.decode("utf-8", errors="replace")[:_MAX_SUBPROCESS_OUTPUT_CHARS]
        err = stderr.decode("utf-8", errors="replace")[:_MAX_SUBPROCESS_OUTPUT_CHARS]
        return f"exit_code={proc.returncode}\nstdout:\n{out}\nstderr:\n{err}"
    except Exception as e:
        return f"Error running shell command: {e}"


# ── Git operations ───────────────────────────────────────────────────────────
# Deliberately narrow: status/diff/log/commit only. No git_push (would need
# ambient credentials — an SSH agent or credential helper — that live outside
# this app's encrypted Connector auth model, unlike every other outbound-write
# path in this app; use create_github_pr instead) and no branch/checkout
# (switching branches invalidates path-based authorization decisions already
# made this session, and can silently destroy uncommitted work).

def _authorize_git_repo(path: str) -> tuple:
    """Return (real_path, None) if `path` is both an authorized directory and
    a git repo (has a .git dir), else (None, error_message)."""
    sanitized = _sanitize_path_input(path or "")
    if not sanitized:
        return None, "Error: path is required (or contains invalid characters)"
    real_path = os.path.realpath(sanitized)
    if _is_blocked_path(real_path):
        return None, f"Error: access denied — system path blocked: {path}"
    if not _authorize_path(real_path):
        return None, f"Error: directory not authorized: {path}{_authorized_dirs_hint()}"
    if not os.path.isdir(os.path.join(real_path, ".git")):
        return None, f"Error: not a git repository (no .git directory): {path}"
    return real_path, None


def _reject_flag_like(value: str, label: str) -> str | None:
    """Return an error if `value` looks like a CLI flag rather than a
    genuine ref/path — list-form subprocess prevents shell injection, but
    not an LLM-supplied value like "--upload-pack=..." being interpreted as
    a git option instead of the argument it's meant to be."""
    if value and value.startswith("-"):
        return f"Error: {label} must not start with '-'"
    return None


# A deliberately nonexistent hooks directory — passed as core.hooksPath so
# git looks for pre-commit/commit-msg/post-commit hooks there instead of the
# repo's real .git/hooks/, finds nothing, and proceeds without running any.
# Only git_commit actually risks triggering a hook (status/diff/log don't),
# but applying it to every call is harmless and one less thing to get wrong.
_GIT_NO_HOOKS_PATH = "/dev/null/orions-belt-no-hooks"

# Repo-local config keys that can make git execute an ARBITRARY,
# attacker-chosen command — none of these can be neutralized by a per-
# invocation `-c key=` override the way core.fsmonitor/core.pager can,
# because the dangerous part is the attacker-chosen VALUE (a filter driver
# name, a gpg program path, a credential helper), not a fixed key. Must be
# detected and refused instead. filter.*.(clean|smudge|process) and
# diff.*.(command|textconv) apply on ordinary `git diff`/`git add`, not just
# checkout — verified against a real repo before this was added.
_DANGEROUS_GIT_CONFIG_RE = re.compile(
    r"^(filter\..*\.(clean|smudge|process)|diff\..*\.(command|textconv)|"
    r"core\.(fsmonitor|sshcommand|alternaterefscommand|askpass|gitproxy|pager)|"
    r"credential\.helper|gpg\.program|uploadpack\..*|pager\..*)$",
    re.IGNORECASE,
    # commit.gpgsign is deliberately NOT here: on its own (without
    # gpg.program also pointed at an attacker-chosen path — which IS
    # matched above) it can't execute anything, it's a completely ordinary
    # setting on any repo where the operator signs commits, and it's
    # already neutralized regardless via the `-c commit.gpgSign=false`
    # override below. Including it here just refused read-only commands
    # (git_status/git_diff) on totally normal repos for no security benefit.
)


async def _git_config_is_safe(cwd: str, env: dict) -> bool:
    """True if `cwd`'s repo-local git config contains no key that could
    result in arbitrary command execution. Listing config (`git config
    --list`) executes nothing itself — it only parses and prints config
    files as text — so this is safe to run unconditionally before every
    git subcommand. Fails CLOSED: any error running the check itself is
    treated as unsafe.

    Deliberately UNSCOPED (no --local): --local alone (a) does NOT reliably
    expand include.path/includeIf despite --includes defaulting to on for
    config lookups — verified against a real repo: a `.git/config` with
    only `[include] path = evil.inc` plus an `evil.inc` containing a
    dangerous filter driver passed this check under `--local --list` while
    the danger was still effective — and (b) excludes the worktree config
    scope (`.git/config.worktree`, active whenever `extensions.worktreeConfig
    = true`), which is a second place a dangerous key can hide entirely
    outside `--local`'s view. An unscoped `git config --list --includes`
    reads system+global+local+worktree+includes — system and global are
    already neutralized by GIT_CONFIG_NOSYSTEM/GIT_CONFIG_GLOBAL in `env`
    (passed to this subprocess exactly as it will be to the real git
    command), so what's left is exactly local+worktree+includes: the same
    effective config the actual git command below will see.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "config", "--list", "--includes", "--null",
            cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        # Exit code 1 with no output means "no config at all" — common and
        # safe, not an error.
        if proc.returncode not in (0, 1):
            return False
        for entry in stdout.decode("utf-8", errors="replace").split("\x00"):
            if not entry:
                continue
            key = entry.split("\n", 1)[0]
            if _DANGEROUS_GIT_CONFIG_RE.match(key):
                log.warning("git tool refused: unsafe config key %r in %s", key, cwd)
                return False
        return True
    except Exception as e:
        log.warning("git config safety check failed for %s: %s", cwd, e)
        return False


async def _run_git(git_args: list, cwd: str, timeout: float = 30,
                   identity: tuple | None = None) -> tuple:
    """Run a git subcommand with the untrusted-repo hardening every git
    tool needs: a maliciously-configured .git/config can make ordinary
    read commands (status/diff/log) execute arbitrary commands via
    core.fsmonitor, diff.external, core.pager, a *.textconv filter, or a
    content filter driver, and a repo's .git/hooks/ can execute arbitrary
    commands on commit — and since create_file (Tier 1) can write
    .git/config or a hook script inside an authorized directory, an
    unhardened git tool would let Tier-1 file write plus a Tier-0/Tier-2
    git operation bypass the Tier-3 approval gate run_shell exists to
    enforce. Some of those (fsmonitor/pager/hooks path) are neutralized by
    a fixed -c override below; others (filter drivers, gpg.program,
    credential helpers) have an attacker-chosen value with no fixed key to
    override, so _git_config_is_safe refuses the whole operation if any of
    those are set at all, rather than trying to neutralize them individually.

    Blocking system/global git config this way also strips out any identity
    (user.name/user.email) an operator configured globally — pass `identity`
    as (name, email) for commit-like operations that need one rather than
    depending on ambient config that's now deliberately unavailable.

    Returns (exit_code, stdout, stderr)."""
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"

    if not await _git_config_is_safe(cwd, env):
        return (1, "", "Error: this repository's local git config contains a setting that "
                       "could execute an arbitrary command (a content filter driver, external "
                       "diff/textconv, credential helper, or gpg program) — refusing to run "
                       "any git command against it.")

    full_args = ["git", "--literal-pathspecs", "--no-pager", "-c", "core.fsmonitor=", "-c", "core.pager=cat",
                 "-c", f"core.hooksPath={_GIT_NO_HOOKS_PATH}", "-c", "commit.gpgSign=false"]
    if identity:
        name, email = identity
        full_args += ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    full_args += git_args
    proc = await asyncio.create_subprocess_exec(
        *full_args, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (proc.returncode,
            stdout.decode("utf-8", errors="replace")[:_MAX_SUBPROCESS_OUTPUT_CHARS],
            stderr.decode("utf-8", errors="replace")[:_MAX_SUBPROCESS_OUTPUT_CHARS])


async def _handle_git_status(tool_name: str, args: dict) -> str:
    """Show the working tree status of a git repo under an authorized directory."""
    real_path, err = _authorize_git_repo(args.get("path", ""))
    if err:
        return err
    try:
        # --ignore-submodules=all: an initialized submodule has its OWN
        # .git/modules/<name>/config and info/attributes, entirely outside
        # what _git_config_is_safe inspects (it only reads the superproject's
        # config) — without this, a dangerous filter/textconv planted only in
        # a submodule's own config still executes when git descends into it
        # to compare content, and none of _run_git's -c overrides propagate
        # into that child git process. This tool has no way to vet a
        # submodule's config, so it must never descend into one at all.
        code, out, stderr = await _run_git(
            ["status", "--porcelain=v1", "-b", "--ignore-submodules=all"], cwd=real_path)
        if code != 0:
            return f"Error: git status failed\n{stderr}"
        # -b always emits a "## <branch>" header line even on a clean tree —
        # "clean" means no lines beyond that one, not an empty result.
        lines = out.splitlines()
        if len(lines) <= 1:
            return f"{out.strip()}\n(clean working tree)" if out.strip() else "(clean working tree)"
        return out
    except asyncio.TimeoutError:
        return "Error: git status timed out"
    except Exception as e:
        return f"Error running git status: {e}"


async def _handle_git_diff(tool_name: str, args: dict) -> str:
    """Show the diff for a git repo under an authorized directory."""
    real_path, err = _authorize_git_repo(args.get("path", ""))
    if err:
        return err
    ref = (args.get("ref") or "").strip()
    if ref:
        flag_err = _reject_flag_like(ref, "ref")
        if flag_err:
            return flag_err

    # --no-ext-diff / --no-textconv: a repo's .git/config can point
    # diff.external or a *.textconv filter at an arbitrary command — those
    # apply even to a read-only `git diff`. -- separates the ref from any
    # path arguments so a crafted ref can't be parsed as a git option.
    # --ignore-submodules=all: see the identical comment in _handle_git_status
    # — --no-ext-diff/--no-textconv do NOT propagate into the child git
    # process git spawns inside a submodule, so a submodule's own config is a
    # complete bypass of both of those unless git never descends into it.
    git_args = ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
    if ref:
        git_args += [ref, "--"]
    try:
        code, out, stderr = await _run_git(git_args, cwd=real_path)
        if code != 0:
            return f"Error: git diff failed\n{stderr}"
        return out or "(no differences)"
    except asyncio.TimeoutError:
        return "Error: git diff timed out"
    except Exception as e:
        return f"Error running git diff: {e}"


async def _handle_git_log(tool_name: str, args: dict) -> str:
    """Show recent commit history for a git repo under an authorized directory."""
    real_path, err = _authorize_git_repo(args.get("path", ""))
    if err:
        return err
    try:
        limit = max(1, min(int(args.get("limit") or 20), 200))
    except (TypeError, ValueError):
        limit = 20

    try:
        code, out, stderr = await _run_git(
            ["log", f"-{limit}", "--pretty=format:%H %ad %an: %s", "--date=short"], cwd=real_path)
        if code != 0:
            return f"Error: git log failed\n{stderr}"
        return out or "(no commits)"
    except asyncio.TimeoutError:
        return "Error: git log timed out"
    except Exception as e:
        return f"Error running git log: {e}"


async def _handle_git_commit(tool_name: str, args: dict) -> str:
    """Stage explicit paths and create a commit in a git repo under an
    authorized directory. Tier 2 — mutates repo state, same class of effect
    as modify_file.

    Takes explicit `paths`, not an "add everything" flag — a blanket
    `git add -A` is how an agent stages and commits a .env or other file it
    never meant to, and every path here is itself re-checked through
    _authorize_path.
    """
    real_path, err = _authorize_git_repo(args.get("path", ""))
    if err:
        return err
    message = (args.get("message") or "").strip()
    if not message:
        return "Error: message is required"
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths:
        return "Error: paths is required and must be a non-empty list of files to commit"

    real_paths = []
    for p in paths:
        sanitized = _sanitize_path_input(str(p))
        if not sanitized:
            return f"Error: invalid path: {p!r}"
        real_p = os.path.realpath(os.path.join(real_path, sanitized) if not os.path.isabs(sanitized) else sanitized)
        if _is_blocked_path(real_p):
            return f"Error: access denied — system path blocked: {p}"
        if not _authorize_path(real_p):
            return f"Error: path not authorized: {p}{_authorized_dirs_hint()}"
        if not real_p.startswith(real_path + os.sep):
            # Also catches real_p == real_path (paths: ["."]) — that's
            # effectively `git add -A`, exactly what taking explicit `paths`
            # instead of an "add everything" flag is meant to prevent.
            return f"Error: path is outside the target repo: {p}"
        if os.path.isdir(real_p):
            return f"Error: path is a directory, not a file — commit explicit files: {p}"
        real_paths.append(os.path.relpath(real_p, real_path))

    # Blocking system/global git config (in _run_git, to stop a malicious
    # .git/config from executing commands) also strips out any operator
    # identity normally set there — pass a fixed one explicitly rather than
    # fail every commit with "unable to auto-detect email address".
    identity = ("Orion's Belt Agent", "agent@orions-belt.local")

    try:
        code, out, stderr = await _run_git(["add", "--"] + real_paths, cwd=real_path)
        if code != 0:
            return f"Error: git add failed\n{stderr}"
        code, out, stderr = await _run_git(
            ["commit", "-m", message, "--"] + real_paths, cwd=real_path, identity=identity)
        if code != 0:
            return f"Error: git commit failed\n{stderr}\n{out}"
        return out
    except asyncio.TimeoutError:
        return "Error: git commit timed out"
    except Exception as e:
        return f"Error running git commit: {e}"


# ── Connector helpers ─────────────────────────────────────────────────────────

def _is_safe_path_segment(value: str) -> bool:
    """True if `value` is safe to interpolate directly into a REST API path.

    These identifiers (repo owner/name, Teams team/channel id, Planner plan/
    bucket id, Salesforce sobject type, ...) are LLM-supplied and go straight
    into a URL path segment. httpx/the provider will normalize a value like
    "../something", so an unchecked value could redirect the request to a
    different endpoint on the same trusted host, still carrying the
    connector's credentials.

    This is a blocklist, not an allowlist of "plausible id characters" — some
    real provider ids are not simple alnum/dash tokens (a Microsoft Graph
    Teams channel id looks like "19:abc123@thread.tacv2"), so an allowlist
    would reject genuine values. What every real id has in common is that it
    never needs a path separator, a ".." segment, or a URL-structural
    character (query string, fragment, percent-encoding) — none of those
    blocked characters appear in a genuine id from any provider this app
    talks to, so refusing them can't reject a real value.
    """
    if not value or "\x00" in value:
        return False
    if "/" in value or "\\" in value:
        return False
    if ".." in value:
        return False
    if any(c in value for c in "?#%"):
        return False
    return not any(c.isspace() for c in value)


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
        "auth": conn.get_auth(),
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
            from app.services.connector_auth import (
                build_auth_headers, validate_action_segment, validate_target_url,
            )

            action_err = validate_action_segment(action)
            if action_err:
                return action_err
            base_url = (config.get("base_url") or "").rstrip("/")
            if not base_url:
                return f"Error: connector '{connector_name}' has no base_url configured"
            url = f"{base_url}/{action}"
            url_err = validate_target_url(url)
            if url_err:
                return url_err
            method = config.get("method", "GET").upper()
            headers = build_auth_headers(config.get("auth_type", "none"), connector.get("auth"))
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(method, url, json=params, headers=headers)
                return f"HTTP {resp.status_code}\n{resp.text[:2000]}"
        elif ctype == "sql_server":
            import pyodbc
            conn = pyodbc.connect(config.get("connection_string", ""))
            cursor = conn.cursor()
            # SECURITY: action is either a SELECT query or a table name.
            # If it looks like a query, enforce SELECT-only. If it's a table
            # name, validate it as a safe identifier before embedding in SQL.
            stripped = re.sub(r"/\*.*?\*/", "", action, flags=re.DOTALL)
            stripped = re.sub(r"--[^\n]*", "", stripped).strip()
            if re.match(r"^SELECT\b", stripped, re.IGNORECASE):
                err = _assert_select_only(action)
                if err:
                    return err
                cursor.execute(action)
            else:
                # Treat as table name — only allow simple identifiers
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_\.]*$", action):
                    return "Error: invalid table name (use simple identifier or SELECT query)"
                safe_table = action.replace("]", "")
                cursor.execute(f"SELECT * FROM [{safe_table}]")
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchmany(50)
            conn.close()
            header = " | ".join(columns)
            return header + "\n" + "\n".join(" | ".join(str(v) for v in row) for row in rows)
        else:
            return f"Error: unsupported connector type: {ctype}"
    except Exception as e:
        return f"Error calling connector: {e}"


async def _handle_create_ado_workitem(tool_name: str, args: dict) -> str:
    """Create a work item (User Story/Bug/Task/etc.) in an Azure DevOps project
    via a configured azure_devops connector.

    Uses the Azure DevOps REST API's work item creation endpoint directly
    (JSON-Patch body, api-version=7.1) rather than the generic call_connector
    rest_api path — ADO's content type (application/json-patch+json) and
    method (POST to a type-specific URL) don't fit that generic GET/POST-
    with-plain-json shape.
    """
    import urllib.parse

    connector_name = args.get("connector", "")
    project = (args.get("project") or "").strip()
    work_item_type = (args.get("work_item_type") or "").strip()
    title = (args.get("title") or "").strip()
    description = args.get("description") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not project:
        return "Error: project is required"
    if not work_item_type:
        return "Error: work_item_type is required (e.g. 'User Story', 'Bug', 'Task')"
    if not title:
        return "Error: title is required"

    connector = _get_connector(connector_name)
    if not connector:
        return f"Error: connector '{connector_name}' not found"
    if connector["type"] != "azure_devops":
        return f"Error: connector '{connector_name}' is not an azure_devops connector"

    config = connector["config"]
    org_url = (config.get("org_url") or "").rstrip("/")
    if not org_url:
        return f"Error: connector '{connector_name}' has no org_url configured"

    pat = (connector.get("auth") or {}).get("pat")
    if not pat:
        return f"Error: connector '{connector_name}' has no personal access token configured"

    from app.services.connector_auth import build_auth_headers, validate_target_url

    url = (
        f"{org_url}/{urllib.parse.quote(project)}/_apis/wit/workitems/"
        f"${urllib.parse.quote(work_item_type)}?api-version=7.1"
    )
    url_err = validate_target_url(url)
    if url_err:
        return url_err

    headers = build_auth_headers("basic", {"username": "", "password": pat})
    headers["Content-Type"] = "application/json-patch+json"

    patch = [{"op": "add", "path": "/fields/System.Title", "value": title}]
    if description:
        patch.append({"op": "add", "path": "/fields/System.Description", "value": description})

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=patch)
        if resp.status_code in (200, 201):
            data = resp.json()
            work_item_id = data.get("id")
            link = f"{org_url}/{urllib.parse.quote(project)}/_workitems/edit/{work_item_id}" if work_item_id else ""
            return f"Created {work_item_type} #{work_item_id} in {project}: {title}\n{link}"
        return f"Error: ADO returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating ADO work item: {e}"


async def _handle_create_github_issue(tool_name: str, args: dict) -> str:
    """Create an issue in a GitHub repository via a configured github connector."""
    connector_name = args.get("connector", "")
    owner = (args.get("owner") or "").strip()
    repo = (args.get("repo") or "").strip()
    title = (args.get("title") or "").strip()
    body = args.get("body") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not owner:
        return "Error: owner is required"
    if not repo:
        return "Error: repo is required"
    if not title:
        return "Error: title is required"
    if not _is_safe_path_segment(owner) or not _is_safe_path_segment(repo):
        return "Error: owner and repo must not contain '/', '\\', whitespace, '..', '?', '#', or '%'"

    connector = _get_connector(connector_name)
    if not connector:
        return f"Error: connector '{connector_name}' not found"
    if connector["type"] != "github":
        return f"Error: connector '{connector_name}' is not a github connector"

    pat = (connector.get("auth") or {}).get("pat")
    if not pat:
        return f"Error: connector '{connector_name}' has no personal access token configured"

    from app.services.connector_auth import build_auth_headers

    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    headers = build_auth_headers("bearer", {"token": pat})
    headers["Accept"] = "application/vnd.github+json"

    payload = {"title": title}
    if body:
        payload["body"] = body

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 201:
            data = resp.json()
            number = data.get("number")
            html_url = data.get("html_url", "")
            return f"Created issue #{number} in {owner}/{repo}: {title}\n{html_url}"
        return f"Error: GitHub returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating GitHub issue: {e}"


def _get_github_connector_and_pat(connector_name: str):
    """Look up a github connector and return (pat, None) or (None, error_message) —
    shared by the PR workflow tools since all three act on the same connector."""
    connector = _get_connector(connector_name)
    if not connector:
        return None, f"Error: connector '{connector_name}' not found"
    if connector["type"] != "github":
        return None, f"Error: connector '{connector_name}' is not a github connector"
    pat = (connector.get("auth") or {}).get("pat")
    if not pat:
        return None, f"Error: connector '{connector_name}' has no personal access token configured"
    return pat, None


async def _handle_create_github_pr(tool_name: str, args: dict) -> str:
    """Create a pull request in a GitHub repository via a configured github connector."""
    connector_name = args.get("connector", "")
    owner = (args.get("owner") or "").strip()
    repo = (args.get("repo") or "").strip()
    title = (args.get("title") or "").strip()
    head = (args.get("head") or "").strip()
    base = (args.get("base") or "").strip()
    body = args.get("body") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not owner:
        return "Error: owner is required"
    if not repo:
        return "Error: repo is required"
    if not title:
        return "Error: title is required"
    if not head:
        return "Error: head is required (the branch containing your changes)"
    if not base:
        return "Error: base is required (the branch you want to merge into)"
    if not _is_safe_path_segment(owner) or not _is_safe_path_segment(repo):
        return "Error: owner and repo must not contain '/', '\\', whitespace, '..', '?', '#', or '%'"

    pat, err = _get_github_connector_and_pat(connector_name)
    if err:
        return err

    from app.services.connector_auth import build_auth_headers
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = build_auth_headers("bearer", {"token": pat})
    headers["Accept"] = "application/vnd.github+json"
    payload = {"title": title, "head": head, "base": base}
    if body:
        payload["body"] = body

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 201:
            data = resp.json()
            number = data.get("number")
            html_url = data.get("html_url", "")
            return f"Created PR #{number} in {owner}/{repo}: {title}\n{html_url}"
        return f"Error: GitHub returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating GitHub PR: {e}"


async def _handle_comment_on_github_pr(tool_name: str, args: dict) -> str:
    """Comment on a pull request via a configured github connector.

    Uses the issues/comments endpoint — GitHub's API treats every PR as an
    issue for comment purposes, there is no separate "PR comment" endpoint
    for a plain top-level comment (as opposed to a line-level review comment).
    """
    connector_name = args.get("connector", "")
    owner = (args.get("owner") or "").strip()
    repo = (args.get("repo") or "").strip()
    pr_number = args.get("pr_number")
    body = (args.get("body") or "").strip()

    if not connector_name:
        return "Error: connector name is required"
    if not owner:
        return "Error: owner is required"
    if not repo:
        return "Error: repo is required"
    if not pr_number:
        return "Error: pr_number is required"
    if not body:
        return "Error: body is required"
    if not _is_safe_path_segment(owner) or not _is_safe_path_segment(repo):
        return "Error: owner and repo must not contain '/', '\\', whitespace, '..', '?', '#', or '%'"
    try:
        pr_number = int(pr_number)
    except (TypeError, ValueError):
        return "Error: pr_number must be an integer"

    pat, err = _get_github_connector_and_pat(connector_name)
    if err:
        return err

    from app.services.connector_auth import build_auth_headers
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    headers = build_auth_headers("bearer", {"token": pat})
    headers["Accept"] = "application/vnd.github+json"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json={"body": body})
        if resp.status_code == 201:
            data = resp.json()
            html_url = data.get("html_url", "")
            return f"Commented on PR #{pr_number} in {owner}/{repo}\n{html_url}"
        return f"Error: GitHub returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error commenting on GitHub PR: {e}"


async def _handle_get_github_pr_status(tool_name: str, args: dict) -> str:
    """Read a pull request's state/mergeable/review status via a configured github connector."""
    connector_name = args.get("connector", "")
    owner = (args.get("owner") or "").strip()
    repo = (args.get("repo") or "").strip()
    pr_number = args.get("pr_number")

    if not connector_name:
        return "Error: connector name is required"
    if not owner:
        return "Error: owner is required"
    if not repo:
        return "Error: repo is required"
    if not pr_number:
        return "Error: pr_number is required"
    if not _is_safe_path_segment(owner) or not _is_safe_path_segment(repo):
        return "Error: owner and repo must not contain '/', '\\', whitespace, '..', '?', '#', or '%'"
    try:
        pr_number = int(pr_number)
    except (TypeError, ValueError):
        return "Error: pr_number must be an integer"

    pat, err = _get_github_connector_and_pat(connector_name)
    if err:
        return err

    from app.services.connector_auth import build_auth_headers
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = build_auth_headers("bearer", {"token": pat})
    headers["Accept"] = "application/vnd.github+json"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            state = data.get("state", "unknown")
            merged = data.get("merged", False)
            mergeable = data.get("mergeable")
            mergeable_state = data.get("mergeable_state", "unknown")
            return (f"PR #{pr_number} in {owner}/{repo}: state={state} merged={merged} "
                    f"mergeable={mergeable} mergeable_state={mergeable_state}")
        return f"Error: GitHub returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error fetching GitHub PR status: {e}"


def _get_jira_connector(connector_name: str):
    """Look up a jira connector and return (base_url, headers, None) or
    (None, None, error_message)."""
    from app.services.connector_auth import build_auth_headers, validate_target_url

    connector = _get_connector(connector_name)
    if not connector:
        return None, None, f"Error: connector '{connector_name}' not found"
    if connector["type"] != "jira":
        return None, None, f"Error: connector '{connector_name}' is not a jira connector"
    base_url = (connector["config"].get("base_url") or "").rstrip("/")
    if not base_url:
        return None, None, f"Error: connector '{connector_name}' has no base_url configured"
    # Same check call_connector already applies to every operator-configured
    # connector base_url — jira's own base_url had been going straight
    # through unchecked, an inconsistency with that existing control even
    # though the value is operator- not LLM-supplied.
    url_err = validate_target_url(base_url)
    if url_err:
        return None, None, url_err
    auth = connector.get("auth") or {}
    if not auth.get("email") or not auth.get("api_token"):
        return None, None, f"Error: connector '{connector_name}' has no email/api_token configured"
    headers = build_auth_headers("basic", {"username": auth["email"], "password": auth["api_token"]})
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/json"
    return base_url, headers, None


async def _handle_create_jira_issue(tool_name: str, args: dict) -> str:
    """Create an issue in a Jira project via a configured jira connector."""
    connector_name = args.get("connector", "")
    project_key = (args.get("project_key") or "").strip()
    issue_type = (args.get("issue_type") or "").strip()
    summary = (args.get("summary") or "").strip()
    description = args.get("description") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not project_key:
        return "Error: project_key is required"
    if not issue_type:
        return "Error: issue_type is required (e.g. 'Task', 'Bug', 'Story')"
    if not summary:
        return "Error: summary is required"

    base_url, headers, err = _get_jira_connector(connector_name)
    if err:
        return err

    payload = {
        "fields": {
            "project": {"key": project_key},
            "issuetype": {"name": issue_type},
            "summary": summary,
        }
    }
    if description:
        # Jira Cloud's v3 API takes description in Atlassian Document Format,
        # not plain text — wrap it in the minimal valid ADF document.
        payload["fields"]["description"] = {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
        }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base_url}/rest/api/3/issue", headers=headers, json=payload)
        if resp.status_code == 201:
            data = resp.json()
            key = data.get("key", "?")
            return f"Created issue {key}: {summary}\n{base_url}/browse/{key}"
        return f"Error: Jira returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating Jira issue: {e}"


_MAX_JIRA_SEARCH_RESULTS = 50


async def _handle_search_jira_issues(tool_name: str, args: dict) -> str:
    """Search Jira issues by JQL via a configured jira connector. Read-only."""
    connector_name = args.get("connector", "")
    jql = (args.get("jql") or "").strip()
    try:
        max_results = max(1, min(int(args.get("max_results") or 20), _MAX_JIRA_SEARCH_RESULTS))
    except (TypeError, ValueError):
        max_results = 20

    if not connector_name:
        return "Error: connector name is required"
    if not jql:
        return "Error: jql is required"

    base_url, headers, err = _get_jira_connector(connector_name)
    if err:
        return err

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Atlassian removed GET /rest/api/3/search on 2025-05-01
            # (announced 2024-10-31, developer.atlassian.com/changelog/#CHANGE-2046)
            # in favor of /rest/api/3/search/jql. Same jql/maxResults/fields
            # params; the only response-shape change is startAt/total being
            # replaced by nextPageToken/isLast, which this handler never read.
            resp = await client.get(f"{base_url}/rest/api/3/search/jql", headers=headers, params={
                "jql": jql, "maxResults": max_results, "fields": "summary,status,issuetype",
            })
        if resp.status_code == 200:
            data = resp.json()
            issues = data.get("issues", [])
            if not issues:
                return "No issues found"
            lines = []
            for issue in issues:
                fields = issue.get("fields", {})
                status = (fields.get("status") or {}).get("name", "?")
                lines.append(f"{issue.get('key')}: {fields.get('summary', '')} [{status}]")
            return "\n".join(lines)
        return f"Error: Jira returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error searching Jira issues: {e}"


def _get_linear_connector(connector_name: str):
    """Look up a linear connector and return (headers, None) or
    (None, error_message)."""
    connector = _get_connector(connector_name)
    if not connector:
        return None, f"Error: connector '{connector_name}' not found"
    if connector["type"] != "linear":
        return None, f"Error: connector '{connector_name}' is not a linear connector"
    auth = connector.get("auth") or {}
    if not auth.get("api_key"):
        return None, f"Error: connector '{connector_name}' has no API key configured"
    # Linear's API takes the raw key as Authorization — no "Bearer " prefix.
    return {"Authorization": auth["api_key"], "Content-Type": "application/json"}, None


async def _handle_create_linear_issue(tool_name: str, args: dict) -> str:
    """Create an issue in Linear via a configured linear connector."""
    connector_name = args.get("connector", "")
    team_id = (args.get("team_id") or "").strip()
    title = (args.get("title") or "").strip()
    description = args.get("description") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not team_id:
        return "Error: team_id is required"
    if not title:
        return "Error: title is required"

    headers, err = _get_linear_connector(connector_name)
    if err:
        return err

    query = {
        "query": "mutation($input: IssueCreateInput!) { issueCreate(input: $input) "
                 "{ success issue { identifier url } } }",
        "variables": {"input": {"teamId": team_id, "title": title, "description": description}},
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post("https://api.linear.app/graphql", headers=headers, json=query)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errors"):
                return f"Error: Linear returned errors: {data['errors']}"
            result = (data.get("data") or {}).get("issueCreate") or {}
            if not result.get("success"):
                return "Error: Linear did not report success creating the issue"
            issue = result.get("issue") or {}
            return f"Created issue {issue.get('identifier', '?')}: {title}\n{issue.get('url', '')}"
        return f"Error: Linear returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating Linear issue: {e}"


_MAX_LINEAR_SEARCH_RESULTS = 50


async def _handle_search_linear_issues(tool_name: str, args: dict) -> str:
    """Search Linear issues via a configured linear connector. Read-only."""
    connector_name = args.get("connector", "")
    search_query = (args.get("query") or "").strip()
    try:
        limit = max(1, min(int(args.get("limit") or 20), _MAX_LINEAR_SEARCH_RESULTS))
    except (TypeError, ValueError):
        limit = 20

    if not connector_name:
        return "Error: connector name is required"
    if not search_query:
        return "Error: query is required"

    headers, err = _get_linear_connector(connector_name)
    if err:
        return err

    gql = {
        "query": "query($filter: IssueFilter!, $first: Int!) { issues(filter: $filter, first: $first) "
                 "{ nodes { identifier title state { name } } } }",
        "variables": {
            "filter": {"title": {"containsIgnoreCase": search_query}},
            "first": limit,
        },
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post("https://api.linear.app/graphql", headers=headers, json=gql)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errors"):
                return f"Error: Linear returned errors: {data['errors']}"
            nodes = ((data.get("data") or {}).get("issues") or {}).get("nodes") or []
            if not nodes:
                return "No issues found"
            lines = [f"{n.get('identifier')}: {n.get('title')} [{(n.get('state') or {}).get('name', '?')}]"
                     for n in nodes]
            return "\n".join(lines)
        return f"Error: Linear returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error searching Linear issues: {e}"


async def _handle_create_google_task(tool_name: str, args: dict) -> str:
    """Create a task in Google Tasks via a configured google connector."""
    from app.models.connector import Connector
    from app.services import oauth
    from app.services.oauth_providers import get_provider_config

    connector_name = args.get("connector", "")
    title = (args.get("title") or "").strip()
    notes = args.get("notes") or ""
    due = args.get("due") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not title:
        return "Error: title is required"

    conn = Connector.query.filter_by(name=connector_name, enabled=True).first()
    if not conn:
        return f"Error: connector '{connector_name}' not found"
    if conn.connector_type != "google":
        return f"Error: connector '{connector_name}' is not a google connector"

    cfg = json.loads(conn.config or "{}")
    provider_cfg = get_provider_config("google", cfg)

    try:
        access_token = oauth.get_valid_access_token(conn, provider_cfg["token_endpoint"])
    except oauth.ReAuthRequired:
        return f"Error: connector '{connector_name}' needs to be reconnected (OAuth consent expired or was never completed)"
    except Exception as e:
        return f"Error: could not obtain a valid Google access token: {e}"

    payload = {"title": title}
    if notes:
        payload["notes"] = notes
    if due:
        payload["due"] = due

    try:
        import httpx
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks",
                headers=headers, json=payload,
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            return f"Created Google Task: {title}\n{data.get('selfLink', '')}"
        return f"Error: Google Tasks API returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating Google Task: {e}"


def _get_graph_connector_and_token(connector_name: str, required_feature: str | None = None):
    """Look up a microsoft_graph connector and return (access_token, None) or
    (None, error_message) — shared by every Graph tool since they all act on
    the same OAuth identity.

    If `required_feature` is given (one of oauth_providers.GRAPH_SCOPE_FEATURES,
    e.g. "calendar"/"files"), checks the connector's last-known granted scope
    actually includes it before making the call — a connector that
    authenticated before a feature's scope existed, or was connected without
    that feature selected, gets a clear "reconnect to grant X access" message
    instead of an opaque Graph API error after the fact. Best-effort: a
    connector with no granted_scope on file yet (never refreshed since that
    started being persisted) skips the check rather than blocking everyone
    retroactively.
    """
    from app.models.connector import Connector
    from app.services import oauth
    from app.services.oauth_providers import GRAPH_SCOPE_FEATURES, get_provider_config

    conn = Connector.query.filter_by(name=connector_name, enabled=True).first()
    if not conn:
        return None, f"Error: connector '{connector_name}' not found"
    if conn.connector_type != "microsoft_graph":
        return None, f"Error: connector '{connector_name}' is not a microsoft_graph connector"

    if required_feature:
        granted = (conn.get_auth() or {}).get("granted_scope")
        needed_perm = GRAPH_SCOPE_FEATURES.get(required_feature)
        if granted and needed_perm and needed_perm not in granted:
            return None, (f"Error: connector '{connector_name}' was not granted {required_feature} access. "
                          f"Reconnect this connector in Settings with '{required_feature}' selected.")

    cfg = json.loads(conn.config or "{}")
    provider_cfg = get_provider_config("microsoft_graph", cfg)

    try:
        access_token = oauth.get_valid_access_token(conn, provider_cfg["token_endpoint"])
    except oauth.ReAuthRequired:
        return None, f"Error: connector '{connector_name}' needs to be reconnected (OAuth consent expired or was never completed)"
    except Exception as e:
        return None, f"Error: could not obtain a valid Microsoft Graph access token: {e}"

    return access_token, None


async def _handle_post_teams_message(tool_name: str, args: dict) -> str:
    """Post a message to a Microsoft Teams channel via a configured microsoft_graph connector."""
    connector_name = args.get("connector", "")
    team_id = (args.get("team_id") or "").strip()
    channel_id = (args.get("channel_id") or "").strip()
    message = (args.get("message") or "").strip()

    if not connector_name:
        return "Error: connector name is required"
    if not team_id:
        return "Error: team_id is required"
    if not channel_id:
        return "Error: channel_id is required"
    if not message:
        return "Error: message is required"
    if not _is_safe_path_segment(team_id) or not _is_safe_path_segment(channel_id):
        return "Error: team_id and channel_id must not contain '/', '\\', whitespace, '..', '?', '#', or '%'"

    access_token, err = _get_graph_connector_and_token(connector_name)
    if err:
        return err

    url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {"body": {"content": message}}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in (200, 201):
            return f"Posted message to Teams channel {channel_id}"
        return f"Error: Microsoft Graph returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error posting Teams message: {e}"


async def _handle_create_planner_task(tool_name: str, args: dict) -> str:
    """Create a task in Microsoft Planner via a configured microsoft_graph connector."""
    connector_name = args.get("connector", "")
    plan_id = (args.get("plan_id") or "").strip()
    title = (args.get("title") or "").strip()
    bucket_id = (args.get("bucket_id") or "").strip()
    due_date_time = args.get("due_date_time") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not plan_id:
        return "Error: plan_id is required"
    if not title:
        return "Error: title is required"

    access_token, err = _get_graph_connector_and_token(connector_name)
    if err:
        return err

    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {"planId": plan_id, "title": title}
    if bucket_id:
        payload["bucketId"] = bucket_id
    if due_date_time:
        payload["dueDateTime"] = due_date_time

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post("https://graph.microsoft.com/v1.0/planner/tasks", headers=headers, json=payload)
        if resp.status_code in (200, 201):
            data = resp.json()
            task_id = data.get("id", "")
            return f"Created Planner task: {title}\n{task_id}"
        return f"Error: Microsoft Graph returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating Planner task: {e}"


async def _handle_create_calendar_event(tool_name: str, args: dict) -> str:
    """Create a calendar event (send a meeting invite) via a configured
    microsoft_graph connector.

    Tier 2 — sends a real invite to real people, the same class of effect
    as send_email/post_teams_message, refused during unattended autonomous
    goal pursuit.
    """
    connector_name = args.get("connector", "")
    subject = (args.get("subject") or "").strip()
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()
    attendees = args.get("attendees") or []
    body = args.get("body") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not subject:
        return "Error: subject is required"
    if not start:
        return "Error: start is required (ISO 8601 datetime, e.g. 2026-08-01T14:00:00)"
    if not end:
        return "Error: end is required (ISO 8601 datetime)"
    if not isinstance(attendees, list):
        return "Error: attendees must be a list of email addresses"

    access_token, err = _get_graph_connector_and_token(connector_name, required_feature="calendar")
    if err:
        return err

    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in attendees],
    }
    if body:
        payload["body"] = {"contentType": "text", "content": body}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post("https://graph.microsoft.com/v1.0/me/events", headers=headers, json=payload)
        if resp.status_code in (200, 201):
            data = resp.json()
            web_link = data.get("webLink", "")
            return f"Created calendar event: {subject}\n{web_link}"
        return f"Error: Microsoft Graph returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating calendar event: {e}"


async def _handle_check_calendar_availability(tool_name: str, args: dict) -> str:
    """Check free/busy availability for a set of attendees via a configured
    microsoft_graph connector. Read-only — Tier 0."""
    connector_name = args.get("connector", "")
    attendees = args.get("attendees") or []
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()

    if not connector_name:
        return "Error: connector name is required"
    if not isinstance(attendees, list) or not attendees:
        return "Error: attendees is required and must be a non-empty list of email addresses"
    if not start:
        return "Error: start is required (ISO 8601 datetime)"
    if not end:
        return "Error: end is required (ISO 8601 datetime)"

    access_token, err = _get_graph_connector_and_token(connector_name, required_feature="calendar")
    if err:
        return err

    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "schedules": attendees,
        "startTime": {"dateTime": start, "timeZone": "UTC"},
        "endTime": {"dateTime": end, "timeZone": "UTC"},
        "availabilityViewInterval": 30,
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post("https://graph.microsoft.com/v1.0/me/calendar/getSchedule",
                                     headers=headers, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            lines = []
            for entry in data.get("value", []):
                email = entry.get("scheduleId", "?")
                view = entry.get("availabilityView", "")
                lines.append(f"{email}: {view or '(no data)'}")
            return "\n".join(lines) if lines else "No availability data returned"
        return f"Error: Microsoft Graph returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error checking calendar availability: {e}"


async def _handle_read_onedrive_file(tool_name: str, args: dict) -> str:
    """Read a file's content from OneDrive via a configured microsoft_graph
    connector (requires the 'files' scope). Tier 0."""
    from urllib.parse import quote

    connector_name = args.get("connector", "")
    path = (args.get("path") or "").strip().lstrip("/")

    if not connector_name:
        return "Error: connector name is required"
    if not path:
        return "Error: path is required"
    if ".." in path.split("/"):
        # Graph resolves the whole quoted string as one path segment inside
        # the connected user's own drive, so ".." can't actually escape to a
        # different drive/tenant — this is defense-in-depth, not a real
        # bypass, but there's no reason to accept it unrejected.
        return f"Error: invalid path (must not contain '..'): {path}"

    access_token, err = _get_graph_connector_and_token(connector_name, required_feature="files")
    if err:
        return err

    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{quote(path)}:/content"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            text = resp.text
            if len(text) > MAX_READ_BYTES:
                text = text[:MAX_READ_BYTES] + "\n[…truncated, file too large]"
            return text
        if resp.status_code == 404:
            return f"Error: file not found: {path}"
        return f"Error: Microsoft Graph returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error reading OneDrive file: {e}"


async def _handle_create_onedrive_file(tool_name: str, args: dict) -> str:
    """Create a new file in OneDrive via a configured microsoft_graph connector
    (requires the 'files' scope). Fails if a file already exists at that path
    — mirrors create_file's semantics. Tier 1."""
    from urllib.parse import quote

    connector_name = args.get("connector", "")
    path = (args.get("path") or "").strip().lstrip("/")
    content = args.get("content") or ""

    if not connector_name:
        return "Error: connector name is required"
    if not path:
        return "Error: path is required"
    if ".." in path.split("/"):
        # See read_onedrive_file's identical check — defense-in-depth, not a
        # real bypass, since Graph resolves this within the user's own drive.
        return f"Error: invalid path (must not contain '..'): {path}"

    access_token, err = _get_graph_connector_and_token(connector_name, required_feature="files")
    if err:
        return err

    headers = {"Authorization": f"Bearer {access_token}"}
    encoded_path = quote(path)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            # PUT :/content overwrites unconditionally — check existence
            # first so this tool's semantics actually match create_file's
            # "fails if the file already exists", not a silent overwrite.
            existing = await client.get(
                f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}", headers=headers)
            if existing.status_code == 200:
                return f"Error: file already exists: {path}"
            if existing.status_code != 404:
                # Anything other than a clean 404 (429 rate-limited, 5xx,
                # auth hiccup, ...) means we genuinely don't know whether the
                # file exists — falling through to PUT here would silently
                # overwrite a real file the existence check merely failed to
                # see. Refuse instead of guessing.
                return (f"Error: could not determine whether {path} already exists "
                        f"(Microsoft Graph returned HTTP {existing.status_code} on the "
                        f"existence check) — refusing to risk an overwrite")
            resp = await client.put(
                f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}:/content",
                headers=headers, content=content.encode("utf-8"))
        if resp.status_code in (200, 201):
            data = resp.json()
            web_url = data.get("webUrl", "")
            return f"Created OneDrive file: {path}\n{web_url}"
        return f"Error: Microsoft Graph returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating OneDrive file: {e}"


def _get_salesforce_connector(connector_name: str):
    """Look up a salesforce connector and return (access_token, instance_url, None)
    or (None, None, error_message) — shared by every Salesforce tool.

    The org's real API host, returned by Salesforce alongside the tokens
    (persisted by oauth.py's _store_tokens/get_valid_access_token), always
    wins over the connector's configured instance_url — that config value
    may be nothing more than the generic login.salesforce.com the user
    authenticated against, which isn't a valid API host after login.
    """
    from app.models.connector import Connector
    from app.services import oauth
    from app.services.oauth_providers import get_provider_config

    conn = Connector.query.filter_by(name=connector_name, enabled=True).first()
    if not conn:
        return None, None, f"Error: connector '{connector_name}' not found"
    if conn.connector_type != "salesforce":
        return None, None, f"Error: connector '{connector_name}' is not a salesforce connector"

    cfg = json.loads(conn.config or "{}")
    provider_cfg = get_provider_config("salesforce", cfg)

    try:
        access_token = oauth.get_valid_access_token(conn, provider_cfg["token_endpoint"])
    except oauth.ReAuthRequired:
        return None, None, f"Error: connector '{connector_name}' needs to be reconnected (OAuth consent expired or was never completed)"
    except Exception as e:
        return None, None, f"Error: could not obtain a valid Salesforce access token: {e}"

    instance_url = (conn.get_auth().get("instance_url") or cfg.get("instance_url")
                    or "https://login.salesforce.com").rstrip("/")
    return access_token, instance_url, None


async def _handle_create_salesforce_record(tool_name: str, args: dict) -> str:
    """Create a record (Lead, Case, Account, etc.) via a configured salesforce connector."""
    connector_name = args.get("connector", "")
    sobject_type = (args.get("sobject_type") or "").strip()
    fields = args.get("fields")

    if not connector_name:
        return "Error: connector name is required"
    if not sobject_type:
        return "Error: sobject_type is required (e.g. 'Lead', 'Case', 'Account')"
    if not isinstance(fields, dict) or not fields:
        return "Error: fields is required and must be a non-empty object of field name -> value"
    if not _is_safe_path_segment(sobject_type):
        return "Error: sobject_type must not contain '/', '\\', whitespace, '..', '?', '#', or '%'"

    access_token, instance_url, err = _get_salesforce_connector(connector_name)
    if err:
        return err

    url = f"{instance_url}/services/data/v59.0/sobjects/{sobject_type}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=fields)
        if resp.status_code == 201:
            data = resp.json()
            record_id = data.get("id", "")
            return f"Created {sobject_type} record: {record_id}\n{instance_url}/{record_id}"
        return f"Error: Salesforce returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error creating Salesforce record: {e}"


_MAX_SOQL_RECORDS = 50


async def _handle_query_salesforce(tool_name: str, args: dict) -> str:
    """Run a read-only SOQL query via a configured salesforce connector.

    Tier 0 — read-only. Capped at _MAX_SOQL_RECORDS records and never
    follows nextRecordsUrl: a bulk SELECT over a CRM's full contact/lead
    table is exactly the kind of thing that shouldn't be a single
    auto-run, unattended-eligible call with no result-size ceiling.

    The query is sent as a `params=` value, never f-strung into the URL —
    that's what actually stops it from escaping into a different endpoint
    via a crafted '&'/'#' (percent-encoding), not a keyword blocklist. SOQL
    has no DML — /query only ever accepts SELECT — so a simple prefix check
    is enough defense-in-depth without the SQL-shaped comment-stripping/
    stacked-query logic _assert_select_only uses for the actual SQL
    connector.

    Results are PII-scanned (fails open, same as every other PII-scan call
    site in this app) before being returned — Salesforce records are the
    highest-PII-density source in this batch (contacts/leads routinely
    carry names, emails, phone numbers), and tool results otherwise reach
    the LLM completely unscanned.
    """
    connector_name = args.get("connector", "")
    soql = (args.get("soql") or "").strip()

    if not connector_name:
        return "Error: connector name is required"
    if not soql:
        return "Error: soql is required"
    if not re.match(r"^\s*SELECT\b", soql, re.IGNORECASE):
        return "Error: only SELECT queries are permitted"

    access_token, instance_url, err = _get_salesforce_connector(connector_name)
    if err:
        return err

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{instance_url}/services/data/v59.0/query",
                                    headers=headers, params={"q": soql})
        if resp.status_code == 200:
            data = resp.json()
            records = data.get("records", [])[:_MAX_SOQL_RECORDS]
            if not records:
                return "No records found"
            lines = [", ".join(f"{k}={v}" for k, v in rec.items() if k != "attributes")
                     for rec in records]
            result = "\n".join(lines)
            total = data.get("totalSize", len(records))
            if total > len(records):
                result += f"\n...[{total - len(records)} more record(s) not shown — refine the query]"

            try:
                from app.services.pii_guard import get_pii_guard
                cleaned, _detected, _types = get_pii_guard().scan(result, direction="outbound")
                result = cleaned
            except Exception as e:
                log.warning("Salesforce query PII scan failed: %s — returning unscanned", e)
            return result
        return f"Error: Salesforce returned HTTP {resp.status_code}\n{resp.text[:1000]}"
    except Exception as e:
        return f"Error querying Salesforce: {e}"


async def _handle_search_emails(tool_name: str, args: dict) -> str:
    """Search Outlook emails."""
    try:
        import win32com.client
    except ImportError:
        return "Error: pywin32 not installed (Windows-only)"

    outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    inbox = outlook.GetDefaultFolder(6)  # 6 = olFolderInbox
    query = (args.get("query") or "").lower()
    try:
        count = int(args.get("count", 20))
    except (ValueError, TypeError):
        count = 20
    count = max(1, min(count, 100))

    items = inbox.Items
    try:
        items.Sort("[ReceivedTime]", True)  # newest first
    except Exception:
        pass

    # Filter FIRST, then take up to `count` matches — the old code sliced the
    # newest `count` emails and searched only those.
    results = []
    for item in items:
        try:
            subject = item.Subject or ""
        except Exception:
            continue  # non-mail item (meeting request, etc.)
        if query and query not in subject.lower():
            continue
        try:
            received = item.ReceivedTime.strftime('%Y-%m-%d')
        except Exception:
            received = "?"
        sender = getattr(item, "SenderName", "") or ""
        results.append(f"  {subject} — {sender} — {received}")
        if len(results) >= count:
            break
    return "\n".join(results) if results else "No matching emails found."


async def _handle_send_email(tool_name: str, args: dict) -> str:
    """Send an email via Outlook COM automation.

    Tier 2 (not Tier 1 like the other create_* tools): an email leaving the
    machine in the user's name is a materially different kind of effect than
    creating a local file, and Tier 2 is already refused under autonomous
    goal pursuit's Tier-1 ceiling while remaining allowed in attended chat —
    exactly the property wanted here with no new tiering logic required.
    """
    to = (args.get("to") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = args.get("body") or ""
    html = args.get("html") or ""
    cc = (args.get("cc") or "").strip()

    if not to:
        return "Error: 'to' is required"
    if not subject:
        return "Error: 'subject' is required"

    try:
        import win32com.client
    except ImportError:
        return "Error: pywin32 not installed (Windows-only)"

    # COM requires apartment-threading setup PER OS THREAD. This tool has
    # only ever run from a request thread or an agent's own background
    # thread, either of which may have had COM implicitly initialized by
    # pywin32's first use — but the scheduled-digest caller fires from
    # triggers.py's dedicated scheduler thread, which has never exercised
    # this path before. pythoncom itself is Windows-only and optional
    # everywhere else (ImportError). Calling CoInitialize() on a thread
    # that's already initialized in the SAME apartment mode is a harmless
    # no-op (S_FALSE) — but one already initialized in a DIFFERENT mode
    # raises pythoncom.com_error (RPC_E_CHANGED_MODE), which is just as
    # benign (the thread already has a COM apartment, which is all
    # Dispatch() needs) but must be caught broadly, not just ImportError,
    # or this would be a regression for every existing caller's thread.
    try:
        import pythoncom
    except ImportError:
        pythoncom = None
    _com_initialized = False
    if pythoncom is not None:
        try:
            pythoncom.CoInitialize()
            _com_initialized = True
        except Exception as e:
            log.debug("send_email: thread already has a COM apartment: %s", e)

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)  # 0 = olMailItem
        mail.To = to
        if cc:
            mail.CC = cc
        mail.Subject = subject
        if html:
            mail.HTMLBody = html
        else:
            mail.Body = body
        mail.Send()
        return f"Email sent to {to}: {subject}"
    except Exception as e:
        return f"Error sending email: {e}"
    finally:
        if _com_initialized:
            pythoncom.CoUninitialize()


async def _handle_http_request(tool_name: str, args: dict) -> str:
    """Make an HTTP request with full control over method, headers, and body.

    Tier 2 (not Tier 1 like fetch_url, its GET-only sibling): this tool can
    send arbitrary POST/PUT/PATCH/DELETE to any allowed host, which is a
    materially different effect than a read-only GET — Tier 2 is refused
    under autonomous goal pursuit's Tier-1 ceiling while remaining allowed
    in attended chat, so an unattended run can't use this to write anywhere.
    """
    from app.services.connector_auth import validate_untrusted_url

    url = (args.get("url") or "").strip()
    method = (args.get("method") or "GET").strip().upper()
    if not url:
        return "Error: url is required"
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return f"Error: unsupported method '{method}'"
    err = validate_untrusted_url(url)
    if err:
        return err

    headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
    body = args.get("body")
    try:
        timeout = min(float(args.get("timeout") or 10), 30)
    except (TypeError, ValueError):
        timeout = 10

    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            kwargs = {"headers": headers}
            if body is not None and method not in ("GET", "HEAD"):
                kwargs["content"] = body if isinstance(body, str) else json.dumps(body)
            status, resp_headers, text, truncated = await _http_fetch_capped(client, method, url, **kwargs)
        if truncated:
            text += "\n...[truncated]"
        content_type = resp_headers.get("content-type", "")
        return f"HTTP {status} ({content_type})\n{text}"
    except httpx.TimeoutException:
        return f"Error: request to {url} timed out after {timeout}s"
    except Exception as e:
        return f"Error making request: {e}"
