# Orion's Belt

> *Three stars. One workbench. Every tool you need.*

A local AI project workbench. Chat with LLMs, manage projects hierarchically, spawn agents that execute real work — all running on your machine with full privacy controls and no data leaving your network.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Privacy & Security](#privacy--security)
- [MCP Tool Tiers](#mcp-tool-tiers)
- [Stack](#stack)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
  - [Linux / macOS](#linux--macos)
  - [Windows](#windows)
  - [Run in Browser Mode](#run-in-browser-mode)
- [Configuration](#configuration)
  - [LLM Providers](#llm-providers)
  - [Environment Variables](#environment-variables)
  - [Authorized Directories](#authorized-directories)
- [Project Structure](#project-structure)
- [API Reference](#api-reference)
- [Database Schema](#database-schema)
- [Services](#services)
  - [PII Guard](#pii-guard)
  - [Memory Service](#memory-service)
  - [Agent Execution](#agent-execution)
  - [MCP Tools](#mcp-tools)
- [Development](#development)
- [Relationship to ORION](#relationship-to-orion)
- [Security Notes](#security-notes)
- [Troubleshooting](#troubleshooting)

---

## What It Does

Orion's Belt structures your AI-assisted work into a hierarchy and keeps every action auditable:

```
You create a Project
  └── Define Epics          (major goals / milestones)
       └── Break into Features   (functional chunks)
            └── Break into Tasks      (executable units)
                 └── Assign to Agents      (autonomous executors)
                      └── Agents use MCP Tools    (file ops, SQL, APIs)
                           └── Results flow back to the Task
```

Use the Chat view to think through problems with the LLM. Use Work to track deliverables. Use Agents to run those deliverables automatically.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Orion's Belt                          │
│                                                         │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────────┐│
│  │  Chat UI │   │ Work UI  │   │  Settings / Logs UI  ││
│  │ (HTMX)   │   │ (HTMX)   │   │  (HTMX)              ││
│  └────┬─────┘   └────┬─────┘   └──────────────────────┘│
│       │              │                                   │
│  ┌────▼──────────────▼──────────────────────────────┐   │
│  │              Flask (Blueprint routes)             │   │
│  │  /chat  /work  /agents  /connectors  /settings   │   │
│  └────────────────────┬──────────────────────────────┘   │
│                       │                                   │
│  ┌────────────────────▼──────────────────────────────┐   │
│  │                 Services Layer                     │   │
│  │  ┌───────────┐ ┌──────────┐ ┌──────────────────┐ │   │
│  │  │ PII Guard │ │  Memory  │ │   Agent Executor  │ │   │
│  │  │ (3-stage) │ │(embeddings│ │  (tool loop)      │ │   │
│  │  └───────────┘ └──────────┘ └──────────────────┘ │   │
│  │  ┌─────────────────────────────────────────────┐  │   │
│  │  │          MCP Tool Executor (tiered auth)     │  │   │
│  │  └─────────────────────────────────────────────┘  │   │
│  └────────────────────┬──────────────────────────────┘   │
│                       │                                   │
│  ┌────────────────────▼──────────────────────────────┐   │
│  │          SQLAlchemy ORM  →  SQLite DB              │   │
│  │  sessions · messages · projects · agents           │   │
│  │  memories · audit_logs · pii_hashes · settings    │   │
│  └────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
         │ SSE / httpx                  ↑ JSON
         ▼                              │
   External LLM API         (clean text only — PII stripped)
   (OpenAI / Ollama /
    any OpenAI-compatible)
```

The application runs as a native desktop window via **pywebview** and lives in the system tray when minimized (like Discord). Flask serves on `127.0.0.1:5000` — never exposed to the network.

---

## Privacy & Security

### PII Guard (3-Stage Pipeline)

Every message is screened before it reaches the LLM:

```
User message
    │
    ▼
Stage 1: Presidio (rule-based)
    Detects: SSN, email, phone, credit card, passport numbers
    │
    ▼
Stage 2: BERT NER (dslim/bert-base-NER)
    Detects: PERSON, ORG, LOC, GPE — contextually
    │
    ▼
Stage 3: DeBERTa zero-shot judge (cross-encoder/nli-deberta-v3-small)
    Classifies ambiguous spans as PHI or not
    │
    ▼
Detected PII → SHA-256 hash → stored in local DB only
Text → [PII:PERSON:a3f9c2d1] → sent to LLM

LLM response → tokens restored → shown to user
```

**No PII or PHI ever leaves the machine.** All models run locally on CPU.

### Data Storage

| Data | Storage | Encrypted |
|------|---------|-----------|
| Conversation history | SQLite | No (local only) |
| PII hash mappings | SQLite | No (hashes only) |
| LLM API keys | SQLite | No — stored as plaintext; **set `SECRET_KEY` env var** and keep DB file permissions tight |
| Connector auth | SQLite | Yes — Fernet symmetric encryption |
| HuggingFace model weights | `./models/` directory | No |
| Audit logs | SQLite | No |

### Path Authorization

File operations require explicit directory authorization (set in Settings → Authorized Directories):

- System paths are always blocked: `C:\Windows`, `C:\Program Files`, etc.
- All paths resolved to real paths before comparison (symlink-safe)
- Null byte injection blocked
- Operations stay within authorized directory boundaries

---

## MCP Tool Tiers

| Tier | Operations | Behavior |
|------|-----------|----------|
| **0 — Auto** | Read file, list directory, search files, search emails, SELECT queries | Silent — no user prompt |
| **1 — Auto + Audit** | Create file, append to file, call connector, INSERT queries | Auto-executes, writes to audit log |
| **2 — Warn** | Modify existing file, create directory, UPDATE queries | 10-second countdown shown in UI; can cancel |
| **3 — Hard Stop** | Delete file, move file, DELETE queries | Pauses execution; requires explicit user approval |

Directories must be authorized in Settings before any file operation can proceed.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+ · Flask 3.x · SQLAlchemy 2.x |
| Database | SQLite (single file, zero config) |
| Frontend | Jinja2 · HTMX · Tailwind CSS (CDN) · Lucide Icons |
| Desktop | pywebview (native window) · pystray (system tray) |
| LLM Client | httpx (OpenAI-compatible streaming) |
| PII Detection | Presidio · BERT NER · DeBERTa zero-shot |
| Memory | sentence-transformers · numpy cosine similarity |
| Connectors | requests/httpx (REST) · pyodbc (SQL Server) · pywin32 (Outlook) |
| Encryption | cryptography (Fernet) |

---

## Requirements

- **Python 3.11+**
- **~1.5 GB disk space** for HuggingFace models (downloaded once on first run):
  - `dslim/bert-base-NER` (~400 MB)
  - `cross-encoder/nli-deberta-v3-small` (~180 MB)
  - `sentence-transformers/all-MiniLM-L6-v2` (~90 MB)
- **RAM**: 4 GB minimum; 8 GB recommended (models run on CPU)
- **Network**: Required for LLM API calls; model download on first run only

---

## Quick Start

### Linux / macOS

```bash
# 1. Clone
git clone https://github.com/richard-callis/orions-belt.git
cd orions-belt

# 2. Set a persistent secret key (important — prevents session loss on restart)
export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 3. Run setup (creates venv, installs deps, downloads spaCy model)
bash setup.sh

# 4. Activate venv and launch
source .venv/bin/activate
python launch.py
```

On first launch, the app opens in a native window. Configure your LLM provider in **Settings** before starting a chat.

### Windows

```cmd
REM Double-click setup.bat OR run in cmd:
setup.bat

REM After setup completes:
run.bat
REM  — or double-click the desktop shortcut created by setup.bat
```

### Run in Browser Mode

If pywebview is unavailable (server, CI, headless):

```bash
python launch.py
# Falls back to opening http://localhost:5000 in your default browser
```

Or run Flask directly:

```bash
flask --app app run --host 127.0.0.1 --port 5000
```

---

## Configuration

### LLM Providers

Configure in the UI at **Settings → LLM Providers**. Supports any OpenAI-compatible endpoint:

| Provider | Base URL | Notes |
|----------|---------|-------|
| OpenAI | `https://api.openai.com/v1` | Requires API key |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/deployments/<deployment>` | API key = Azure key |
| Ollama (local) | `http://localhost:11434/v1` | No API key needed |
| LM Studio | `http://localhost:1234/v1` | No API key needed |
| llama-server | `http://localhost:8080/v1` | No API key needed |
| Any OpenAI-compatible | Custom URL | Set model name accordingly |

Multiple providers can be saved and switched between in the UI.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | Random (changes each restart) | Flask session key. **Set this** or sessions break on restart |
| `PII_HASH_SALT` | `orions-belt-pii` | Salt for PII SHA-256 hashing. Change before first run to customize |
| `HF_HOME` | `./models` | HuggingFace model cache directory |
| `TRANSFORMERS_CACHE` | `./models/hub` | Transformers-specific cache |

Create a `.env` file in the project root (it is `.gitignore`d):

```bash
SECRET_KEY=your-32-char-hex-secret-here
PII_HASH_SALT=your-custom-salt-here
```

### Authorized Directories

File operations require explicit authorization. In the UI: **Settings → Authorized Directories → Add Directory**.

- Set an **alias** (what the LLM sees, e.g. `project_files`)
- Set the **real path** on disk
- Toggle **read-only** to prevent any writes
- Set **max tier** to cap the operations allowed in that directory
- Set an **expiration date** for temporary grants

---

## Project Structure

```
orions-belt/
├── app/
│   ├── __init__.py          # Flask app factory, blueprint registration
│   ├── models/
│   │   ├── chat.py          # Session, Message, ContextCompaction
│   │   ├── work.py          # Project, Epic, Feature, Task
│   │   ├── agent.py         # Agent, AgentRun, AgentStep
│   │   ├── connector.py     # Connector, AuthorizedDirectory
│   │   ├── mcp_tool.py      # MCPTool, ToolProposal
│   │   ├── memory.py        # Memory (with embeddings)
│   │   ├── logs.py          # AuditLog, PIILog, AgentLog, LLMLog
│   │   ├── pii.py           # PIIHashEntry (local hash map)
│   │   └── settings.py      # Setting (key-value store)
│   ├── routes/
│   │   ├── chat.py          # Chat sessions + SSE streaming
│   │   ├── work.py          # Projects / Epics / Features / Tasks
│   │   ├── agents.py        # Agent management + run control
│   │   ├── connectors.py    # Connector CRUD
│   │   ├── mcp.py           # MCP tool management
│   │   ├── memory.py        # Memory management + search
│   │   ├── logs.py          # Log viewer
│   │   └── settings.py      # LLM config, health check, settings API
│   ├── services/
│   │   ├── llm.py           # Context building + tool definitions
│   │   ├── mcp/
│   │   │   └── tools.py     # Tool execution with tier-based auth (12 built-in tools)
│   │   ├── pii_guard/
│   │   │   └── __init__.py  # PIIGuard — 3-stage PII detection pipeline
│   │   ├── memory/
│   │   │   └── __init__.py  # MemoryService — embeddings + similarity recall
│   │   └── agents/
│   │       └── __init__.py  # AgentExecutor — tool loop + approval flow
│   ├── templates/
│   │   ├── base.html        # Layout, nav, sidebar
│   │   ├── chat.html        # Chat UI (SSE streaming)
│   │   ├── work.html        # Project hierarchy
│   │   ├── agents.html      # Agent list + configuration
│   │   ├── connectors.html  # Connector management
│   │   ├── mcp.html         # MCP tool list + tier badges
│   │   ├── memory.html      # Memory viewer + search
│   │   ├── logs.html        # Audit / PII / Agent / LLM logs
│   │   └── settings.html    # LLM providers + directories + debug
│   └── static/
│       └── css/custom.css   # ORION design system (dark theme, animations)
├── config.py                # Configuration class
├── launch.py                # Desktop launcher (pywebview + pystray)
├── download_models.py       # Pre-download HuggingFace models
├── create_icon.py           # Generate tray icon
├── requirements.txt         # Python dependencies
├── setup.sh                 # Linux/macOS setup script
├── setup.bat                # Windows setup script
├── run.bat                  # Windows launcher
├── start_silent.vbs         # Windows silent launcher (no console)
├── logs/                    # Application logs (created at startup)
├── models/                  # HuggingFace model cache (created at startup)
└── orions_belt.db           # SQLite database (created at startup, gitignored)
```

---

## API Reference

All endpoints served on `http://127.0.0.1:5000`.

### Health

| Method | Endpoint | Description |
|--------|---------|-------------|
| `GET` | `/api/health` | Service health + component status |

### Chat

| Method | Endpoint | Description |
|--------|---------|-------------|
| `GET` | `/api/sessions` | List recent sessions (50 max) |
| `POST` | `/api/sessions` | Create new session |
| `PATCH` | `/api/sessions/<id>` | Rename session |
| `DELETE` | `/api/sessions/<id>` | Delete session and all messages |
| `GET` | `/api/sessions/<id>/messages` | Get message history |
| `POST` | `/api/sessions/<id>/stream` | Stream LLM response (SSE) |

**SSE Event Types** (from `/stream`):

```
event: text         data: {"delta": "..."}
event: tool_call    data: {"name": "read_file", "args": {...}}
event: tool_result  data: {"name": "read_file", "result": "..."}
event: done         data: {"total_tokens": 1234}
event: error        data: {"message": "..."}
```

### Settings

| Method | Endpoint | Description |
|--------|---------|-------------|
| `GET` | `/api/settings` | List all settings (API keys redacted) |
| `GET` | `/api/settings/<key>` | Get single setting value |
| `PUT` | `/api/settings/<key>` | Set setting value |

### Projects / Work

| Method | Endpoint | Description |
|--------|---------|-------------|
| `GET` | `/api/projects` | List all projects |
| `POST` | `/api/projects` | Create project |
| `GET` | `/api/projects/<id>/epics` | List epics |
| `POST` | `/api/projects/<id>/epics` | Create epic |
| `GET` | `/api/epics/<id>/features` | List features |
| `POST` | `/api/epics/<id>/features` | Create feature |
| `GET` | `/api/features/<id>/tasks` | List tasks |
| `POST` | `/api/features/<id>/tasks` | Create task |
| `PATCH` | `/api/tasks/<id>` | Update task status / assignment |

### Agents

| Method | Endpoint | Description |
|--------|---------|-------------|
| `GET` | `/api/agents` | List agents |
| `POST` | `/api/agents` | Create agent |
| `PATCH` | `/api/agents/<id>` | Update agent configuration |
| `DELETE` | `/api/agents/<id>` | Delete agent |
| `POST` | `/api/agents/<id>/run` | Start agent run on a task |
| `GET` | `/api/agent-runs/<id>` | Get run status and steps |
| `POST` | `/api/agent-steps/<id>/approve` | Approve a Tier 3 pending step |

### Memory

| Method | Endpoint | Description |
|--------|---------|-------------|
| `GET` | `/api/memories` | List memories (filter by `?type=`) |
| `POST` | `/api/memories` | Store new memory |
| `DELETE` | `/api/memories/<id>` | Delete memory |
| `GET` | `/api/memories/search?q=<query>` | Semantic search via embedding similarity |

---

## Database Schema

14 tables in `orions_belt.db`:

| Table | Purpose |
|-------|---------|
| `sessions` | Chat sessions |
| `messages` | Individual chat messages (with PII flags, token counts) |
| `context_compactions` | Records of context window compression events |
| `projects` | Top-level projects |
| `epics` | Major goal groupings within a project |
| `features` | Functional chunks within an epic |
| `tasks` | Executable work items within a feature |
| `agents` | Agent definitions (system prompt, allowed tools, model) |
| `agent_runs` | Individual agent execution records |
| `agent_steps` | Each tool call within an agent run |
| `connectors` | External system connections (REST, SQL, Outlook) |
| `authorized_directories` | Whitelisted file system paths |
| `mcp_tools` | Tool definitions with tier and enable/disable flag |
| `tool_proposals` | Audit trail of proposed/approved/rejected tools |
| `memories` | Cross-session persistent memories with embeddings |
| `audit_logs` | Every MCP tool call: caller, tier, outcome |
| `pii_logs` | PII detection events (direction, entity types) |
| `agent_logs` | Agent execution events |
| `llm_logs` | LLM API calls (provider, model, tokens, latency, cost) |
| `pii_hash_entries` | SHA-256 hash ↔ original value mappings (local only) |
| `settings` | Key-value configuration store |

---

## Services

### PII Guard

`app/services/pii_guard/`

Three-stage detection pipeline that screens all outbound text:

```python
from app.services.pii_guard import get_pii_guard

guard = get_pii_guard()

# Scan text before sending to LLM
clean_text, pii_found, entity_types = guard.scan(
    text="My name is John Smith, SSN 123-45-6789",
    session_id="abc",
    direction="outbound"
)
# clean_text → "My name is [PII:PERSON:a3f9], SSN [PII:SSN:b7c2]"

# Restore for display (uses local hash table)
original = guard.restore(clean_text)
```

Models load lazily on first call. Graceful degradation: Presidio-only if transformers fail; pass-through if all fail.

### Memory Service

`app/services/memory/`

Persistent cross-session memory with semantic recall:

```python
from app.services.memory import get_memory_service

mem = get_memory_service()

# Store a fact
mem.store(
    title="User prefers Python",
    content="The user is a Python developer and prefers async patterns",
    memory_type="persistent",
    source="user"
)

# Recall relevant memories for a query
memories = mem.recall("what language should I use?", top_k=5)

# Inject as system context (returns formatted string)
context = mem.inject_context("what language should I use?", session_id="abc")
```

### Agent Execution

`app/services/agents/`

Autonomous tool loop with tier-based approval:

```python
from app.services.agents import run_agent

# Start agent on a task (returns immediately, run is async)
agent_run = run_agent(agent_id=1, task_id=42)

# Check status
# run.status: pending | running | awaiting_approval | completed | failed | cancelled
```

Runs up to `agent.max_iterations` tool calls. Pauses at Tier 3 operations for human approval.

### MCP Tools

`app/services/mcp/tools.py`

12 built-in tools, executed with tier-based authorization:

| Tool | Tier | Description |
|------|------|-------------|
| `read_file` | 0 | Read file from authorized directory |
| `list_directory` | 0 | List files in authorized directory |
| `search_files` | 0 | Find files matching a pattern |
| `search_emails` | 0 | Search Outlook emails (read-only) |
| `run_sql_query` | 1 | Execute SELECT via SQL connector |
| `create_file` | 1 | Create a new file (fails if exists) |
| `append_to_file` | 1 | Append content to existing file |
| `call_connector` | 1 | Call a configured REST/SQL connector |
| `modify_file` | 2 | Overwrite an existing file |
| `create_directory` | 2 | Create a new directory |
| `delete_file` | 3 | Delete a file (requires approval) |
| `move_file` | 3 | Move or rename a file (requires approval) |

---

## Development

```bash
# Run Flask in dev mode (hot reload, debug errors in browser)
export FLASK_DEBUG=1
flask --app app run --host 127.0.0.1 --port 5000

# Pre-download all HuggingFace models (avoids first-run delay)
python download_models.py

# Reset database (start fresh)
rm orions_belt.db
python launch.py

# Re-generate the app icon
python create_icon.py
```

### Adding a New MCP Tool

1. Add the tool definition to `_seed_builtin_tools()` in `launch.py`
2. Add a `_handle_<tool_name>` function in `app/services/mcp/tools.py`
3. Add a case to the `execute_tool()` dispatch in the same file
4. Set the appropriate tier (0–3)

### Adding a New LLM Provider

Any OpenAI-compatible API works. In the UI: Settings → LLM Providers → Add Provider. The base URL and model name are all that's required (API key optional for local models).

---

## Relationship to ORION

```
ORION (Raspberry Pi cluster)        Orion's Belt (your laptop/desktop)
────────────────────────────        ──────────────────────────────────
Manages Kubernetes cluster     ↔    Manages your work
Provisions infrastructure           Projects + Epics + Tasks
GitOps / ArgoCD                     Local file + data operations
Remote control                      Local AI execution
```

Same ORION design language (`#0f0f0f` / `#00A7E1`). Different mission.

---

## Security Notes

**This is a local single-user application.** It runs on `127.0.0.1` and is not designed for multi-user or network-exposed deployments.

- **No authentication** by default (localhost only). Do not expose port 5000 externally.
- **API keys** are stored in plaintext SQLite. Set filesystem permissions on `orions_belt.db` appropriately (`chmod 600`).
- **SECRET_KEY** defaults to a random value that changes each restart (breaks sessions). Set it via env var for persistence.
- **PII hashes** use SHA-256 with a configurable salt. The hash map lives in the local DB and never leaves the machine.
- **File operations** are gated by the authorized directory whitelist. No operation can escape outside an authorized path.
- **Connector credentials** are encrypted at rest with Fernet symmetric encryption.
- **Tool proposals** go through AST scanning and optional LLM safety review before approval.

---

## Troubleshooting

**Flask won't start / port conflict:**
```bash
lsof -i :5000      # Linux/macOS — find what's using port 5000
netstat -ano | findstr :5000   # Windows
```

**HuggingFace models fail to download:**
```bash
# Run the model downloader directly with verbose output
python download_models.py
# If behind a proxy, set HF_HUB_VERBOSITY=debug
```

**spaCy model missing:**
```bash
python -m spacy download en_core_web_lg
```

**Database corruption / migration issues:**
```bash
rm orions_belt.db   # Nuclear option — fresh start, all data lost
python launch.py
```

**pywebview won't open (Linux):**
```bash
# Install WebKit2 GTK
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.0
# Or fall back to browser mode:
python launch.py   # Falls back automatically if pywebview fails
```

**Windows: pywin32 import error:**
```cmd
pip install pywin32
python -m win32com.client.makepy   # Register COM types
```

---

*Named for the three stars of Orion's Belt: Alnitak · Alnilam · Mintaka*
*Three pillars: Work · Agents · Connectors*
