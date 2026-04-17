# Orion's Belt

> *Three stars. One workbench. Every tool you need.*

A local AI project workbench. Create projects, discuss them with an LLM, spawn agents that execute the work — all running on your machine, with full privacy controls.

## What it is

Orion's Belt is a companion to [ORION](https://github.com/richard-callis/orion-web) — where ORION manages infrastructure, Orion's Belt manages **your work**.

```
You create a Project
  └── Define Epics (big goals)
       └── Break into Features
            └── Break into Tasks
                 └── Assign to Agents
                      └── Agents use MCP tools to execute
                           └── Results flow back to the Task
```

Chat with the LLM to shape the project. Let agents do the execution.

## Core Features

| Feature | Description |
|---|---|
| **Chat** | Session-based conversations with context tracking + compaction |
| **Projects** | Epic → Feature → Task hierarchy |
| **Agents** | Assigned to tasks, execute via MCP tools, report back |
| **MCP Tools** | File ops, SQL queries, connectors — tiered authorization |
| **Connectors** | Custom REST APIs, Outlook, SQL Server |
| **PII Guard** | All outbound data screened — sensitive data hashed locally, never leaves machine |
| **Memory** | Cross-session persistent memory with similarity recall |
| **Logs** | Audit, PII, Agent, and LLM call logs |

## Privacy Architecture

```
Your data → Presidio (rules) → BERT NER → local llama.cpp judge
           → PII detected → hashed as [PII:TYPE:abc123] stored locally
           → clean text only → external LLM
```

No PII/PHI leaves the machine.

## MCP Tool Tiers

| Tier | Operations | Behavior |
|---|---|---|
| 0 — Auto | Read, list, SELECT | Silent |
| 1 — Auto + Audit | Create new file, INSERT | Logged |
| 2 — Warn | Modify existing file, UPDATE | 10s countdown |
| 3 — Hard Stop | Delete, move, DELETE | Explicit approval |

Directories must be explicitly authorized. System paths are always blocked.

## Stack

- **Backend**: Python + Flask + SQLAlchemy
- **Database**: SQLite (single file, zero setup)
- **Frontend**: Jinja2 + HTMX + Tailwind CSS (CDN — no build step)
- **Launcher**: pywebview + pystray (runs like Discord — native window, system tray)
- **LLM**: OpenAI-compatible API or llama-server + llama-cpp-python for local model
- **Colors**: ORION design system (`#0f0f0f` / `#00A7E1`)

## Setup

```bash
# Clone
git clone https://github.com/richard-callis/orions-belt.git
cd orions-belt

# Install dependencies
pip install -r requirements.txt
python -m spacy download en_core_web_lg

# Run
python launch.py
```

On first launch, Orion's Belt opens a native window and walks you through:
1. LLM provider configuration (OpenAI / llama-server / custom endpoint)
2. Local model path for PII guard (optional GGUF file)
3. Authorized directories for file operations

## Relationship to ORION

```
ORION (RPi)          Orion's Belt (Windows laptop)
─────────────        ──────────────────────────────
Manages cluster  ←→  Manages your work
Infrastructure       Projects + Tasks + Agents
GitOps               Local file + data ops
Remote control       Local execution
```

Same visual design language. Different mission.

---

*Named for the three stars of Orion's Belt: Alnitak · Alnilam · Mintaka*
*Three pillars: Work · Agents · Connectors*
