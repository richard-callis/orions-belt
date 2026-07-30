"""
Orion's Belt — Desktop Launcher
Starts Flask in a background thread, opens a native pywebview window.
Sits in the system tray when minimized (like Discord).
"""
import sys
import os
import atexit
import threading
import time
import logging
import logging.handlers
from pathlib import Path
from config import Config

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
    from sqlalchemy import event

    # ── Automated recovery: restore from .bak if DB is corrupted ──────
    from app.services.backup import recover_if_needed, has_valid_backup, get_backup_path, get_db_path
    if not recover_if_needed():
        log.warning("Recovery unavailable — starting with potentially corrupt/missing database")
        if has_valid_backup():
            log.warning("A valid backup exists at %s — manual restore may be needed", get_backup_path())

    app = create_app()
    with app.app_context():
        # Enable WAL mode BEFORE any database operations so all connections
        # use write-ahead logging from the start.
        @event.listens_for(db.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
        # WAL + busy_timeout prevent corruption and blocking on concurrent writes

        db.create_all()
        _migrate_llm_settings(app)
        _seed_novas(app)
        _ensure_projects_dir(app)
        _migrate_schema(app)
        _seed_builtin_tools(app)
        _seed_agents(app)

    # Register on-shutdown backup + periodic scheduled backups
    from app.services.backup import register_shutdown_backup, start_periodic_backups
    from app.services.retention import start_retention_service, stop_retention_service
    from app.services.dream import start_dream_service, stop_dream_service
    from app.services.db_crypto import set_db_path, enforce_file_permissions
    from config import Config

    # Set DB path for permission enforcement
    set_db_path(Path(Config.DATABASE_PATH))
    enforce_file_permissions()

    # Start data retention service
    atexit.register(stop_retention_service)
    start_retention_service(interval_hours=6.0)

    # Dream (lessons-learned extraction) — off by default (agents.dream_enabled
    # setting), started unconditionally like the other periodic services but a
    # no-op every tick until the user opts in via Settings.
    atexit.register(stop_dream_service)
    start_dream_service(interval_hours=2.0)

    register_shutdown_backup()
    start_periodic_backups(interval_minutes=30)

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
                    "path": {"type": "string", "description": "Absolute path to list — must be under one of the authorized directories listed in your system prompt"},
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
            name="create_ado_workitem",
            tier=1,
            description="Create a work item (User Story, Bug, Task, etc.) in an Azure DevOps project via a configured azure_devops connector",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "connector": {"type": "string", "description": "Azure DevOps connector name as configured in Settings"},
                    "project": {"type": "string", "description": "Azure DevOps project name"},
                    "work_item_type": {"type": "string", "description": "Work item type, e.g. 'User Story', 'Bug', 'Task'"},
                    "title": {"type": "string", "description": "Work item title"},
                    "description": {"type": "string", "description": "Work item description (optional)"},
                },
                "required": ["connector", "project", "work_item_type", "title"],
            }),
        ),
        dict(
            name="create_github_issue",
            tier=1,
            description="Create an issue in a GitHub repository via a configured github connector",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "connector": {"type": "string", "description": "GitHub connector name as configured in Settings"},
                    "owner": {"type": "string", "description": "Repository owner (user or organization)"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body (optional)"},
                },
                "required": ["connector", "owner", "repo", "title"],
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
            name="send_email",
            tier=2,
            description="Send an email via Outlook (Windows only). Tier 2 — refused during "
                        "unattended autonomous goal pursuit, allowed in attended chat.",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address(es), semicolon-separated for multiple"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body text"},
                    "cc": {"type": "string", "description": "CC address(es), optional"},
                },
                "required": ["to", "subject", "body"],
            }),
        ),
        dict(
            name="create_word_document",
            tier=1,
            description="Create a Word (.docx) document from plain text. Paragraphs separated "
                        "by a blank line; '# '/'## '/'### ' prefixes become headings; '- '/'* ' "
                        "prefixed lines become a bullet list.",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the .docx file to create"},
                    "title": {"type": "string", "description": "Optional document title (top heading)"},
                    "content": {"type": "string", "description": "Document body text"},
                },
                "required": ["path", "content"],
            }),
        ),
        dict(
            name="create_powerpoint",
            tier=1,
            description="Create a PowerPoint (.pptx) presentation from a list of slides, "
                        "each with a title and bullet points.",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the .pptx file to create"},
                    "title": {"type": "string", "description": "Optional title-slide title"},
                    "subtitle": {"type": "string", "description": "Optional title-slide subtitle"},
                    "slides": {
                        "type": "array",
                        "description": "List of {title, bullets: [str, ...]}",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
                "required": ["path", "slides"],
            }),
        ),
        dict(
            name="create_excel",
            tier=1,
            description="Create an Excel (.xlsx) workbook. Supports a single sheet "
                        "({headers, rows}) or multiple named sheets.",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the .xlsx file to create"},
                    "sheet_name": {"type": "string", "description": "Sheet name when providing a single sheet (default 'Sheet1')"},
                    "sheets": {
                        "type": "object",
                        "description": "{headers: [...], rows: [[...], ...]} for one sheet, OR "
                                        "{SheetName: {headers, rows}, ...} for multiple sheets",
                    },
                },
                "required": ["path", "sheets"],
            }),
        ),
        dict(
            name="create_pdf",
            tier=1,
            description="Create a PDF document from plain text, with the same "
                        "heading/bullet conventions as create_word_document.",
            input_schema=json.dumps({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the .pdf file to create"},
                    "title": {"type": "string", "description": "Optional document title"},
                    "content": {"type": "string", "description": "Document body text"},
                },
                "required": ["path", "content"],
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
                    "path": {"type": "string", "description": "Absolute path to create — must be under one of the authorized directories listed in your system prompt"},
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


def _seed_novas(app):
    """Seed bundled Nova templates. Safe to call on every startup — skips existing names."""
    import json
    from app.models.nova import Nova
    from app import db

    bundled = [
        # ── Agent Novas ───────────────────────────────────────────────────────
        dict(
            name="code_reviewer",
            display_name="Code Reviewer",
            nova_type="agent",
            category="DevOps",
            description="Reviews code for bugs, security issues, style problems, and test coverage gaps. Reads files and produces structured feedback.",
            tags=["code", "review", "quality", "security"],
            config={
                "system_prompt": (
                    "You are a senior code reviewer. When given a file or set of files:\n"
                    "1. Check for bugs, logic errors, and edge cases\n"
                    "2. Flag security vulnerabilities (injection, hardcoded secrets, unsafe deserialization)\n"
                    "3. Note style and readability issues\n"
                    "4. Identify missing test coverage\n"
                    "5. Suggest concrete improvements with code examples\n\n"
                    "Be specific — cite file names and line numbers. Prioritize critical issues first."
                ),
                "allowed_tools": ["read_file", "list_directory", "search_files"],
                "max_iterations": 15,
            },
        ),
        dict(
            name="sql_analyst",
            display_name="SQL Analyst",
            nova_type="agent",
            category="Data",
            description="Queries SQL databases, interprets results, and produces clear summaries and insights. Works with any configured SQL connector.",
            tags=["sql", "database", "analysis", "reporting"],
            config={
                "system_prompt": (
                    "You are a SQL data analyst. Your job is to:\n"
                    "1. Understand the user's data question\n"
                    "2. Write safe, read-only SELECT queries to answer it\n"
                    "3. Interpret the results clearly — use plain language, not raw data dumps\n"
                    "4. Highlight anomalies, trends, or surprising findings\n"
                    "5. Suggest follow-up queries if the answer raises new questions\n\n"
                    "Always use LIMIT clauses. Never run UPDATE, DELETE, INSERT, or DDL statements."
                ),
                "allowed_tools": ["run_sql_query", "call_connector"],
                "max_iterations": 20,
            },
        ),
        dict(
            name="documentation_writer",
            display_name="Documentation Writer",
            nova_type="agent",
            category="Writing",
            description="Reads source code and existing docs, then writes or updates clear technical documentation in Markdown.",
            tags=["docs", "writing", "markdown", "technical"],
            config={
                "system_prompt": (
                    "You are a technical writer specializing in developer documentation.\n"
                    "When asked to document something:\n"
                    "1. Read the relevant source files to understand the actual implementation\n"
                    "2. Write clear, concise Markdown documentation\n"
                    "3. Include: purpose, parameters/fields, return values, usage examples\n"
                    "4. Keep it accurate — document what the code does, not what it should do\n"
                    "5. Use consistent headings, code fences, and formatting\n\n"
                    "Prefer updating existing docs over creating new files when possible."
                ),
                "allowed_tools": ["read_file", "list_directory", "search_files", "create_file", "modify_file"],
                "max_iterations": 20,
            },
        ),
        dict(
            name="bug_triager",
            display_name="Bug Triager",
            nova_type="agent",
            category="DevOps",
            description="Analyzes bug reports, reproduces issues by reading code, identifies root causes, and suggests fixes.",
            tags=["bugs", "debugging", "triage", "root-cause"],
            config={
                "system_prompt": (
                    "You are a debugging specialist. When given a bug report:\n"
                    "1. Read the relevant source files to understand the code path\n"
                    "2. Identify the root cause — be specific about what line or logic is wrong\n"
                    "3. Assess severity (critical/high/medium/low) and explain why\n"
                    "4. Propose a fix with a code snippet\n"
                    "5. Identify any related code that might have the same bug\n\n"
                    "Always read the code before forming a hypothesis. Do not guess."
                ),
                "allowed_tools": ["read_file", "list_directory", "search_files"],
                "max_iterations": 20,
            },
        ),
        dict(
            name="research_assistant",
            display_name="Research Assistant",
            nova_type="agent",
            category="Research",
            description="Gathers information from files, databases, and notes to compile thorough research reports.",
            tags=["research", "synthesis", "reports", "analysis"],
            config={
                "system_prompt": (
                    "You are a research assistant. When given a research question:\n"
                    "1. Identify what information sources are available (files, databases, notes)\n"
                    "2. Systematically gather relevant information\n"
                    "3. Synthesize findings into a structured report\n"
                    "4. Cite your sources (file names, query results)\n"
                    "5. Distinguish between confirmed facts and inferences\n"
                    "6. End with a clear summary and any open questions\n\n"
                    "Be thorough but concise. Quality over quantity."
                ),
                "allowed_tools": ["read_file", "list_directory", "search_files", "run_sql_query"],
                "max_iterations": 25,
            },
        ),
        dict(
            name="project_planner",
            display_name="Project Planner",
            nova_type="agent",
            category="Productivity",
            description="Breaks down a high-level goal into a structured plan with epics, features, and actionable tasks.",
            tags=["planning", "project management", "breakdown", "tasks"],
            config={
                "system_prompt": (
                    "You are a project planning specialist. When given a goal or feature request:\n"
                    "1. Clarify scope and success criteria\n"
                    "2. Identify major work streams (Epics)\n"
                    "3. Break each Epic into Features (functional chunks)\n"
                    "4. Break each Feature into concrete Tasks with clear acceptance criteria\n"
                    "5. Flag dependencies and risks\n"
                    "6. Estimate relative complexity (S/M/L/XL)\n\n"
                    "Output a clean hierarchical plan. Be realistic about scope — "
                    "prefer smaller, shippable increments over big-bang deliveries."
                ),
                "allowed_tools": ["read_file", "search_files"],
                "max_iterations": 15,
            },
        ),

        # ── Connector Novas ───────────────────────────────────────────────────
        dict(
            name="connector_github",
            display_name="GitHub REST API",
            nova_type="connector",
            category="DevTools",
            description="GitHub REST API v3. Access repos, issues, pull requests, commits, and actions. Requires a Personal Access Token.",
            tags=["github", "git", "repos", "issues", "ci"],
            config={
                "connector_type": "rest_api",
                "base_url": "https://api.github.com",
                "auth_type": "bearer",
                "headers": {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                "notes": "Set Authorization header to 'Bearer <your_pat>'",
            },
        ),
        dict(
            name="connector_jira",
            display_name="Jira REST API",
            nova_type="connector",
            category="DevTools",
            description="Jira Cloud REST API v3. Query issues, sprints, projects, and boards. Requires Atlassian API token.",
            tags=["jira", "issues", "sprints", "atlassian", "project management"],
            config={
                "connector_type": "rest_api",
                "base_url": "https://your-domain.atlassian.net/rest/api/3",
                "auth_type": "basic",
                "headers": {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                "notes": "Replace 'your-domain' with your Atlassian domain. Use email:api_token as Basic auth.",
            },
        ),
        dict(
            name="connector_linear",
            display_name="Linear API",
            nova_type="connector",
            category="DevTools",
            description="Linear GraphQL API. Access issues, cycles, projects, and teams. Requires a Linear API key.",
            tags=["linear", "issues", "project management", "graphql"],
            config={
                "connector_type": "rest_api",
                "base_url": "https://api.linear.app/graphql",
                "auth_type": "bearer",
                "headers": {
                    "Content-Type": "application/json",
                },
                "notes": "All requests are POST with a 'query' JSON body. Set Authorization to your Linear API key.",
            },
        ),
        dict(
            name="connector_slack",
            display_name="Slack Web API",
            nova_type="connector",
            category="Messaging",
            description="Slack Web API. Post messages, read channels, search messages. Requires a Slack Bot Token (xoxb-).",
            tags=["slack", "messaging", "notifications", "chat"],
            config={
                "connector_type": "rest_api",
                "base_url": "https://slack.com/api",
                "auth_type": "bearer",
                "headers": {
                    "Content-Type": "application/json; charset=utf-8",
                },
                "notes": "Set Authorization to 'Bearer xoxb-your-bot-token'",
            },
        ),
        dict(
            name="connector_postgres",
            display_name="PostgreSQL Database",
            nova_type="connector",
            category="Database",
            description="PostgreSQL database connector template. Update the connection string with your host, port, database, and credentials.",
            tags=["postgresql", "sql", "database", "postgres"],
            config={
                "connector_type": "sql_server",
                "driver": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "your_database",
                "notes": "Fill in host, port, database, username, and password in the connector config.",
            },
        ),

        # ── MCP Tool Novas ────────────────────────────────────────────────────
        dict(
            name="mcp_web_fetcher",
            display_name="Web Fetcher",
            nova_type="mcp_tool",
            category="Web",
            description="Adds a fetch_url tool — agents can retrieve content from any HTTP/HTTPS URL and return the response body.",
            tags=["web", "http", "scraping", "fetch"],
            config={
                "tools": [
                    {
                        "name": "fetch_url",
                        "description": "Fetch the content of an HTTP/HTTPS URL and return the response body as text",
                        "tier": 1,
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "The full URL to fetch (http or https)"},
                                "method": {"type": "string", "description": "HTTP method: GET (default) or POST"},
                                "headers": {"type": "object", "description": "Optional HTTP headers as key-value pairs"},
                                "body": {"type": "string", "description": "Optional request body (for POST)"},
                                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                            },
                            "required": ["url"],
                        },
                    },
                ],
            },
        ),
        dict(
            name="mcp_python_runner",
            display_name="Python Runner",
            nova_type="mcp_tool",
            category="Code Execution",
            description="Adds a run_python tool — agents can execute Python snippets in a sandboxed subprocess and capture stdout/stderr.",
            tags=["python", "code execution", "scripting", "compute"],
            config={
                "tools": [
                    {
                        "name": "run_python",
                        "description": "Execute a Python code snippet in a subprocess and return stdout and stderr",
                        "tier": 2,
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string", "description": "Python source code to execute"},
                                "timeout": {"type": "integer", "description": "Execution timeout in seconds (default 30, max 120)"},
                            },
                            "required": ["code"],
                        },
                    },
                ],
            },
        ),
        dict(
            name="mcp_shell_runner",
            display_name="Shell Command Runner",
            nova_type="mcp_tool",
            category="Shell",
            description="Adds a run_shell tool — agents can execute shell commands. Tier 3: requires explicit human approval before each run.",
            tags=["shell", "bash", "cli", "commands", "system"],
            config={
                "tools": [
                    {
                        "name": "run_shell",
                        "description": "Execute a shell command and return stdout and stderr. Requires explicit approval (Tier 3).",
                        "tier": 3,
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "description": "The shell command to execute"},
                                "working_dir": {"type": "string", "description": "Working directory for the command (default: project root)"},
                                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)"},
                            },
                            "required": ["command"],
                        },
                    },
                ],
            },
        ),
        dict(
            name="mcp_http_request",
            display_name="HTTP Request",
            nova_type="mcp_tool",
            category="Web",
            description="Adds an http_request tool — full-featured HTTP client with header control, body, and response inspection.",
            tags=["http", "api", "rest", "request"],
            config={
                "tools": [
                    {
                        "name": "http_request",
                        "description": "Make an HTTP request with full control over method, headers, and body",
                        "tier": 1,
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "url":     {"type": "string",  "description": "Full URL"},
                                "method":  {"type": "string",  "description": "HTTP method: GET, POST, PUT, PATCH, DELETE"},
                                "headers": {"type": "object",  "description": "Request headers"},
                                "body":    {"type": "string",  "description": "Request body (JSON string or plain text)"},
                                "timeout": {"type": "integer", "description": "Timeout seconds (default 30)"},
                            },
                            "required": ["url", "method"],
                        },
                    },
                ],
            },
        ),

        # ── Workflow Novas ────────────────────────────────────────────────────
        dict(
            name="workflow_software_release",
            display_name="Software Feature Release",
            nova_type="workflow",
            category="Software",
            description="End-to-end workflow for shipping a new software feature: design, build, test, and release.",
            tags=["release", "software", "feature", "deployment"],
            config={
                "epics": [
                    {
                        "title": "Design & Planning",
                        "description": "Define scope, design the solution, and plan the work",
                        "features": [
                            {
                                "title": "Requirements & Acceptance Criteria",
                                "tasks": [
                                    {"title": "Document feature requirements"},
                                    {"title": "Define acceptance criteria"},
                                    {"title": "Get stakeholder sign-off"},
                                ],
                            },
                            {
                                "title": "Technical Design",
                                "tasks": [
                                    {"title": "Design data model changes"},
                                    {"title": "Design API contract"},
                                    {"title": "Review design with team"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Build",
                        "description": "Implement the feature",
                        "features": [
                            {
                                "title": "Backend Implementation",
                                "tasks": [
                                    {"title": "Implement data model / migrations"},
                                    {"title": "Implement business logic"},
                                    {"title": "Implement API endpoints"},
                                ],
                            },
                            {
                                "title": "Frontend Implementation",
                                "tasks": [
                                    {"title": "Build UI components"},
                                    {"title": "Wire up API integration"},
                                    {"title": "Handle error and loading states"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Quality Assurance",
                        "description": "Test, review, and harden the implementation",
                        "features": [
                            {
                                "title": "Testing",
                                "tasks": [
                                    {"title": "Write unit tests"},
                                    {"title": "Write integration tests"},
                                    {"title": "Manual QA against acceptance criteria"},
                                ],
                            },
                            {
                                "title": "Code Review & Hardening",
                                "tasks": [
                                    {"title": "Code review"},
                                    {"title": "Address review feedback"},
                                    {"title": "Security review"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Release",
                        "description": "Ship and monitor the feature",
                        "features": [
                            {
                                "title": "Deployment",
                                "tasks": [
                                    {"title": "Deploy to staging"},
                                    {"title": "Smoke test staging"},
                                    {"title": "Deploy to production"},
                                ],
                            },
                            {
                                "title": "Post-Release",
                                "tasks": [
                                    {"title": "Monitor error rates and performance"},
                                    {"title": "Update documentation"},
                                    {"title": "Close feature ticket and notify stakeholders"},
                                ],
                            },
                        ],
                    },
                ],
            },
        ),
        dict(
            name="workflow_bug_fix",
            display_name="Bug Fix Sprint",
            nova_type="workflow",
            category="Software",
            description="Structured workflow for triaging, fixing, and verifying a bug from report to production.",
            tags=["bug", "fix", "debugging", "hotfix"],
            config={
                "epics": [
                    {
                        "title": "Triage & Reproduce",
                        "features": [
                            {
                                "title": "Bug Investigation",
                                "tasks": [
                                    {"title": "Reproduce the bug locally"},
                                    {"title": "Identify root cause"},
                                    {"title": "Assess severity and impact"},
                                    {"title": "Document reproduction steps"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Fix",
                        "features": [
                            {
                                "title": "Implementation",
                                "tasks": [
                                    {"title": "Implement the fix"},
                                    {"title": "Write regression test"},
                                    {"title": "Check for related bugs in same code path"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Verify & Ship",
                        "features": [
                            {
                                "title": "Verification",
                                "tasks": [
                                    {"title": "Verify fix resolves the bug"},
                                    {"title": "Run full test suite"},
                                    {"title": "Code review"},
                                    {"title": "Deploy and confirm in production"},
                                ],
                            },
                        ],
                    },
                ],
            },
        ),
        dict(
            name="workflow_data_analysis",
            display_name="Data Analysis Project",
            nova_type="workflow",
            category="Data",
            description="End-to-end data analysis workflow: collect, clean, analyse, and report findings.",
            tags=["data", "analysis", "reporting", "insights"],
            config={
                "epics": [
                    {
                        "title": "Data Collection",
                        "features": [
                            {
                                "title": "Source Identification & Access",
                                "tasks": [
                                    {"title": "Identify data sources"},
                                    {"title": "Set up connector / access credentials"},
                                    {"title": "Perform initial data pull"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Data Preparation",
                        "features": [
                            {
                                "title": "Cleaning & Validation",
                                "tasks": [
                                    {"title": "Profile data — check nulls, types, ranges"},
                                    {"title": "Handle missing values"},
                                    {"title": "Remove duplicates and outliers"},
                                    {"title": "Validate against business rules"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Analysis",
                        "features": [
                            {
                                "title": "Exploratory Analysis",
                                "tasks": [
                                    {"title": "Compute summary statistics"},
                                    {"title": "Identify trends and patterns"},
                                    {"title": "Test hypotheses"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Reporting",
                        "features": [
                            {
                                "title": "Output & Communication",
                                "tasks": [
                                    {"title": "Write findings summary"},
                                    {"title": "Create visualizations or tables"},
                                    {"title": "Document methodology"},
                                    {"title": "Present findings to stakeholders"},
                                ],
                            },
                        ],
                    },
                ],
            },
        ),
        dict(
            name="workflow_api_integration",
            display_name="API Integration Project",
            nova_type="workflow",
            category="Software",
            description="Workflow for integrating an external API: explore, build, test, and document the integration.",
            tags=["api", "integration", "connector", "REST"],
            config={
                "epics": [
                    {
                        "title": "Exploration",
                        "features": [
                            {
                                "title": "API Discovery",
                                "tasks": [
                                    {"title": "Review API documentation"},
                                    {"title": "Obtain API credentials / keys"},
                                    {"title": "Test API manually with sample calls"},
                                    {"title": "Identify rate limits and constraints"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Build Integration",
                        "features": [
                            {
                                "title": "Connector Setup",
                                "tasks": [
                                    {"title": "Configure connector in Orion's Belt"},
                                    {"title": "Implement authentication flow"},
                                    {"title": "Build required API calls"},
                                ],
                            },
                            {
                                "title": "Business Logic",
                                "tasks": [
                                    {"title": "Map API data to internal models"},
                                    {"title": "Handle errors and retries"},
                                    {"title": "Implement data transformation"},
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Testing & Documentation",
                        "features": [
                            {
                                "title": "Validation",
                                "tasks": [
                                    {"title": "Write integration tests"},
                                    {"title": "Test error handling and edge cases"},
                                    {"title": "Document connector usage"},
                                ],
                            },
                        ],
                    },
                ],
            },
        ),
    ]

    for nova_def in bundled:
        tags   = nova_def.pop("tags",   [])
        config = nova_def.pop("config", {})
        existing = Nova.query.filter_by(name=nova_def["name"]).first()
        if not existing:
            db.session.add(Nova(
                id=str(__import__("uuid").uuid4()),
                source="bundled",
                tags=json.dumps(tags),
                config=json.dumps(config),
                **nova_def,
            ))
    db.session.commit()


def _seed_agents(app):
    """Seed default live Agent rows so chat rooms have real agents to add on
    first run, not just Nova templates. Safe to call on every startup — skips
    existing names (Agent.name has no DB uniqueness constraint, so this
    filter-first check is what keeps it idempotent).
    """
    import json
    from app.models.agent import Agent
    from app.models.nova import Nova
    from app import db

    # Reuse the project_planner Nova's own system_prompt rather than
    # duplicating it by hand — falls back to an inline prompt if that Nova
    # was ever removed/renamed.
    planner_nova = Nova.query.filter_by(name="project_planner").first()
    if planner_nova:
        planner_prompt = json.loads(planner_nova.config or "{}").get("system_prompt", "")
    else:
        planner_prompt = (
            "You are a project planning specialist. When given a goal or feature request, "
            "clarify scope, break it into epics/features/tasks, flag dependencies and risks, "
            "and estimate relative complexity (S/M/L/XL)."
        )

    defaults = [
        dict(
            name="Project Planner",
            description="Breaks down a high-level goal into a structured plan with epics, features, and tasks.",
            system_prompt=planner_prompt,
            # Widened beyond the project_planner Nova's read-only tool list so
            # this agent can also produce a shareable plan document.
            allowed_tools=["read_file", "search_files", "create_word_document", "create_excel"],
            max_iterations=15,
        ),
        dict(
            name="Email Assistant",
            description="Searches Outlook email to answer questions and surface relevant messages.",
            system_prompt=(
                "You are an email assistant. Use search_emails to find messages relevant to the "
                "user's request and summarize what you find clearly. You can only search and read "
                "email — you cannot send, reply to, or delete messages."
            ),
            allowed_tools=["search_emails"],
            max_iterations=10,
        ),
        dict(
            name="Document Writer",
            description="Creates Word documents, PowerPoint decks, Excel workbooks, and PDFs from plain-text content.",
            system_prompt=(
                "You are a document production specialist. When asked to produce a deliverable, "
                "pick the right format for the content (Word for reports/memos, PowerPoint for "
                "presentations, Excel for tabular data, PDF for fixed-layout documents) and use the "
                "matching create_* tool. Read source files first if the content needs to come from "
                "somewhere else in the project."
            ),
            allowed_tools=["create_word_document", "create_powerpoint", "create_excel", "create_pdf", "read_file"],
            max_iterations=15,
        ),
        dict(
            name="Research Assistant",
            description="Gathers information from files, databases, and notes to compile thorough research reports.",
            system_prompt=(
                "You are a research assistant. When given a research question:\n"
                "1. Identify what information sources are available (files, databases, notes)\n"
                "2. Systematically gather relevant information\n"
                "3. Synthesize findings into a structured report\n"
                "4. Cite your sources (file names, query results)\n"
                "5. Distinguish between confirmed facts and inferences\n"
                "6. End with a clear summary and any open questions\n\n"
                "Be thorough but concise. Quality over quantity."
            ),
            allowed_tools=["read_file", "list_directory", "search_files", "run_sql_query"],
            max_iterations=25,
        ),
    ]

    for agent_def in defaults:
        existing = Agent.query.filter_by(name=agent_def["name"]).first()
        if not existing:
            allowed_tools = agent_def.pop("allowed_tools")
            db.session.add(Agent(allowed_tools=json.dumps(allowed_tools), **agent_def))
    db.session.commit()


def _column_add_ddl(column, dialect) -> str:
    """Build a SQLite `ALTER TABLE ADD COLUMN` body for a model column.

    Adds the column as nullable unless it has a usable default, because SQLite
    cannot add a NOT NULL column without a default. NOT NULL/values on new rows
    are still enforced by the ORM; existing rows simply get NULL/the default.
    """
    type_sql = column.type.compile(dialect=dialect)
    ddl = f"{column.name} {type_sql}"

    default_sql = None
    if column.server_default is not None:
        arg = getattr(column.server_default, "arg", None)
        text = getattr(arg, "text", None)
        default_sql = text if text is not None else (str(arg) if arg is not None else None)
    elif column.default is not None and getattr(column.default, "is_scalar", False):
        val = column.default.arg
        if isinstance(val, bool):
            default_sql = "1" if val else "0"
        elif isinstance(val, (int, float)):
            default_sql = str(val)
        elif isinstance(val, str):
            default_sql = "'" + val.replace("'", "''") + "'"

    if default_sql is not None:
        ddl += f" DEFAULT {default_sql}"
        if not column.nullable:
            ddl += " NOT NULL"
    return ddl


def _migrate_schema(app):
    """Reconcile the on-disk schema with the models (idempotent).

    create_all() only creates missing TABLES, never missing COLUMNS on tables
    that already exist. So a database created before a column was added to a
    model would be missing that column, raising "no such column" at runtime.
    This introspects every mapped table and ALTER-adds any column the model
    declares but the database lacks — so column additions never need a
    hand-maintained migration list again.
    """
    from app import db

    # Indexes on hot FK columns (idempotent; also declared index=True on the
    # models so fresh installs get them via create_all).
    indexes = [
        ("ix_messages_session_id",           "messages",            "session_id"),
        ("ix_agent_runs_agent_id",           "agent_runs",          "agent_id"),
        ("ix_agent_steps_run_id",            "agent_steps",         "run_id"),
        ("ix_token_usage_agent_id",          "token_usage",         "agent_id"),
        ("ix_chat_room_messages_room_id",    "chat_room_messages",  "room_id"),
    ]

    with db.engine.connect() as conn:
        dialect = conn.dialect
        # Which tables actually exist on disk.
        db_tables = {
            row[0] for row in conn.execute(db.text(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ))
        }
        for table in db.metadata.sorted_tables:
            if table.name not in db_tables:
                continue  # create_all handles brand-new tables
            existing = {
                row[1]
                for row in conn.execute(db.text(f"PRAGMA table_info({table.name})"))
            }
            for column in table.columns:
                if column.name in existing:
                    continue
                try:
                    ddl = _column_add_ddl(column, dialect)
                    conn.execute(db.text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
                    print(f"[migrate] added {table.name}.{column.name}")
                except Exception as e:
                    print(f"[migrate] could not add {table.name}.{column.name}: {e}")

        for idx_name, table_name, column in indexes:
            try:
                conn.execute(db.text(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name} ({column})"
                ))
            except Exception as e:
                print(f"[migrate] index {idx_name} skipped: {e}")
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
    """Clean up corrupted LLM provider data and encrypt plaintext API keys."""
    import json
    from app import db
    from app.models.settings import Setting

    # Remove old flat keys that are no longer used
    for key in ("llm.base_url", "llm.api_key", "llm.model", "llm.provider"):
        row = db.session.get(Setting, key)
        if row:
            db.session.delete(row)

    # Fix corrupted llm.providers and encrypt plaintext keys
    row = db.session.get(Setting, "llm.providers")
    if row:
        try:
            providers = json.loads(row.value)
            if not isinstance(providers, list):
                raise ValueError("not a list")
            from app.services.crypto import encrypt_data

            plaintext_prefixes = ("sk-", "sk-proj-", "ghp_", "glpat-", "xoxb-", "xoxp-", "AIza", "EA")
            for p in providers:
                raw_key = p.get("api_key", "")
                if raw_key and raw_key.startswith(plaintext_prefixes) and len(raw_key) > 10:
                    encrypted = encrypt_data(raw_key)
                    if encrypted:
                        p["api_key"] = encrypted
                        print(f"[migrate] encrypted API key for provider: {p.get('name', '?')}")
            row.value = json.dumps(providers)
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
