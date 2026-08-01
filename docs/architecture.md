# Architecture

## Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Orion's Belt                          │
│                                                           │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────────┐  │
│  │ Chat UI  │  │ Work UI  │  │ Settings / Logs UI     │  │
│  │ (HTMX)   │  │ (HTMX)   │  │ (HTMX)                 │  │
│  └────┬─────┘  └────┬─────┘  └───────────────────────┘  │
│       │             │                                    │
│  ┌────▼─────────────▼───────────────────────────────┐   │
│  │           Flask (18 blueprint routes)             │   │
│  │  /chat  /work  /agents  /connectors  /settings …  │   │
│  └────────────────────┬───────────────────────────────┘  │
│                        │                                  │
│  ┌─────────────────────▼──────────────────────────────┐  │
│  │                 Services Layer                      │  │
│  │  ┌───────────┐ ┌──────────┐ ┌────────────────────┐ │  │
│  │  │ PII Guard │ │  Memory  │ │   Agent Executor   │ │  │
│  │  │ (3-stage) │ │(embedding│ │   (tool loop)       │ │  │
│  │  └───────────┘ └──────────┘ └────────────────────┘ │  │
│  │  ┌──────────────────────────────────────────────┐  │  │
│  │  │      MCP Tool Executor (tiered auth)          │  │  │
│  │  └──────────────────────────────────────────────┘  │  │
│  └─────────────────────┬──────────────────────────────┘  │
│                        │                                  │
│  ┌─────────────────────▼──────────────────────────────┐  │
│  │            SQLAlchemy ORM → SQLite                  │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
         │ SSE / httpx                  ↑ JSON
         ▼                              │
   External LLM API         (clean text only — PII stripped)
   (OpenAI / Ollama / any OpenAI-compatible endpoint)
```

The app runs as a native desktop window via **pywebview** and lives in the system tray when minimized. Flask serves on `127.0.0.1:5000` — never exposed to the network.

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+ · Flask 3.x · SQLAlchemy 2.x |
| Database | SQLite (single file, zero config) |
| Frontend | Jinja2 · HTMX · a vendored, pre-built Tailwind CSS bundle · Lucide Icons |
| Desktop | pywebview (native window) · pystray (system tray) |
| LLM client | httpx (OpenAI-compatible streaming) |
| PII detection | Presidio · GLiNER · DeBERTa zero-shot |
| Memory | sentence-transformers · NumPy cosine similarity (optional LanceDB acceleration) |
| Connectors | httpx/requests (REST + OAuth) · pyodbc (SQL Server) · pywin32 (Outlook, Windows only) |
| Encryption | `cryptography` (Fernet) |

## Services

### PII Guard — `app/services/pii_guard/`

A three-stage pipeline screens every outbound message before it reaches an LLM:

```
Stage 1 — Presidio (rule-based)       SSN, email, phone, credit card, passport
Stage 2 — GLiNER (zero-shot NER)      PERSON, ORG, LOC, GPE — contextual, any casing
Stage 3 — DeBERTa zero-shot judge     classifies ambiguous spans as PHI or not
```

Detected entities are SHA-256 hashed and stored locally; the LLM only ever sees a placeholder token (`[PII:PERSON:a3f9c2d1]`). Responses are restored from the local hash table before being shown to the user. **No PII or PHI leaves the machine** — every stage runs on CPU, locally.

Degrades gracefully: Presidio-only if the transformer models fail to load; pass-through (with a warning) if everything fails.

```python
from app.services.pii_guard import get_pii_guard

guard = get_pii_guard()
clean_text, pii_found, entity_types = guard.scan(
    text="My name is John Smith, SSN 123-45-6789",
    session_id="abc", direction="outbound",
)
original = guard.restore(clean_text)
```

### Memory Service — `app/services/memory/`

Persistent, cross-session memory with semantic recall via sentence-transformer embeddings, cosine-similarity search over SQLite-stored vectors (LanceDB accelerates this when installed; NumPy is the always-available fallback).

```python
from app.services.memory import get_memory_service

mem = get_memory_service()
mem.store(title="User prefers Python", content="...", memory_type="persistent", source="user")
memories = mem.recall("what language should I use?", top_k=5)
context = mem.inject_context("what language should I use?", session_id="abc")
```

### Agent Execution — `app/services/agents/`

An autonomous tool-calling loop, capped at `agent.max_iterations`, that pauses for human approval at Tier 3 (hard-stop) operations.

```python
from app.services.agents import run_agent

agent_run = run_agent(agent_id=1, task_id=42)
# run.status: pending | running | awaiting_approval | completed | failed | cancelled
```

### MCP Tool Executor — `app/services/mcp/tools.py`

Every tool call — whether from a chat message or an autonomous agent — goes through one authorization chokepoint keyed by **tier**:

| Tier | Behavior | Examples |
|---|---|---|
| **0 — Auto** | Silent, no prompt | Read files, list directories, SELECT queries, search Jira/Linear, git status/diff |
| **1 — Auto + Audit** | Executes immediately, logged | Create/append files, call a connector, git commit, create a GitHub issue |
| **2 — Warn** | Countdown shown in UI, cancellable | Overwrite a file, send email, post to Teams, git commit into a shared repo |
| **3 — Hard stop** | Execution pauses for explicit approval | Delete/move files, run arbitrary shell commands |

Directories must be explicitly authorized in **Settings → Authorized Directories** before any file tool can touch them — with independent `read_only` and `max_tier` caps per directory. Tool tiers and schemas are visible live in **MCP Tools** in the app; that page is the source of truth rather than a hand-maintained list here, since the tool count changes often (dozens of tools across git operations, GitHub/Jira/Linear/Salesforce/Microsoft Graph connectors, document indexing, code execution, and office-document generation, in addition to the original file/SQL/email primitives).

## Database

SQLAlchemy models, grouped by domain (`app/models/`):

| Domain | Models |
|---|---|
| Chat | `Session`, `Message`, `ContextCompaction`, `ChatRoom`, `ChatRoomMember`, `ChatRoomMessage`, `ChatRoomGoal`, `PendingToolApproval` |
| Work | `Project`, `Epic`, `Feature`, `Task` |
| Agents | `Agent`, `AgentRun`, `AgentStep`, `TokenUsage` |
| Nova (reusable templates) | `Nova` |
| Tools & authorization | `MCPTool`, `ToolProposal`, `Connector`, `AuthorizedDirectory` |
| Memory & knowledge | `Memory`, `Note`, `DocumentChunk` |
| Scheduling | `ScheduledTrigger`, `DigestSchedule` |
| Review pipelines | `DreamLesson` |
| Logs & audit | `AuditLog`, `PIILog`, `AgentLog`, `AgentTrace`, `LLMLog` |
| Privacy | `PIIHashEntry`, `PIIException` |
| Auth & config | `User`, `Setting` |

All data lives in a single `orions_belt.db` SQLite file. See [Security Notes](../README.md#security-notes) for what's encrypted vs. plaintext.
