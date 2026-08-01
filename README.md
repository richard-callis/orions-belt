# Orion's Belt

> Three stars. One workbench. Every tool you need.

A local AI project workbench. Chat with LLMs, manage projects hierarchically, and spawn agents that execute real work — entirely on your machine, with layered privacy controls and no data leaving your network unless you explicitly configure a connector to an external service.

[![Tests](https://github.com/richard-callis/orions-belt/actions/workflows/tests.yml/badge.svg)](https://github.com/richard-callis/orions-belt/actions/workflows/tests.yml)

---

## Contents

- [What it does](#what-it-does)
- [Key features](#key-features)
- [Privacy & security](#privacy--security)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Project layout](#project-layout)
- [Documentation](#documentation)
- [Relationship to ORION](#relationship-to-orion)
- [Security notes](#security-notes)

## What it does

Orion's Belt structures AI-assisted work into a hierarchy and keeps every action auditable:

```
Project → Epics (milestones) → Features (chunks) → Tasks (units) → Agents (executors) → MCP Tools (file ops, SQL, APIs, connectors)
```

Use **Chat** to think through problems with an LLM. Use **Work** to track deliverables. Use **Agents** to run those deliverables autonomously, with every tool call gated by a tier system you control.

## Key features

- **Tiered tool authorization** — every action an LLM or agent takes (read a file, call an API, delete something) is classified 0–3 by risk and handled accordingly: silent, audited, warned-with-countdown, or paused for explicit approval.
- **Local PII/PHI screening** — a three-stage detection pipeline strips sensitive data before it ever reaches an external LLM API, restoring it only for local display.
- **Native connectors** — GitHub, Jira, Linear, Microsoft Graph (Teams/Planner/Calendar/OneDrive), Salesforce, Google, Azure DevOps, plus generic REST and SQL Server connectors.
- **Autonomous agents** — a bounded tool-calling loop that can pursue a goal across multiple rounds, backed by scheduled triggers and digest emails for unattended operation.
- **Persistent memory & document search** — semantic recall across sessions, plus on-demand search over indexed local documents.
- **Nova templates** — reusable, shareable definitions for agents, connectors, MCP tools, and workflows.
- **Runs as a real desktop app** — native window via pywebview, system tray, single installer for Windows or Linux/macOS.

See [`docs/architecture.md`](docs/architecture.md) for how these pieces fit together.

## Privacy & security

Every message is screened before it reaches an LLM:

```
Presidio (rule-based)  →  GLiNER (zero-shot NER)  →  DeBERTa (ambiguous-span judge)
        detects: SSN, email, phone, PERSON, ORG, LOC, GPE, and more
```

Detected values are SHA-256 hashed and kept in the local database only; the LLM sees a placeholder token, never the original text. **No PII or PHI leaves the machine** — every model in the pipeline runs locally on CPU.

File operations are confined to directories you explicitly authorize, each with its own read-only flag and tier cap. See [Security notes](#security-notes) below and [`docs/architecture.md`](docs/architecture.md) for the full pipeline and data-storage breakdown.

## Requirements

**Windows — prebuilt exe (recommended, no install)**
Download `OrionsBelt.exe` from the [latest release](https://github.com/richard-callis/orions-belt/releases/latest). ~1.5 GB disk for models (downloaded on first launch), 4 GB RAM minimum (8 GB recommended), network access for LLM calls and the one-time model download.

**Linux / macOS / Windows from source**
Python 3.11+, same disk/RAM/network requirements as above. Models are cached locally after the first run:
`gliner_medium-v2.1` (~400 MB), `nli-deberta-v3-small` (~180 MB), `all-MiniLM-L6-v2` (~90 MB).

## Quick start

### Linux / macOS

```bash
git clone https://github.com/richard-callis/orions-belt.git
cd orions-belt

# Set a persistent secret key — without this, sessions break on every restart
export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

bash run.sh
```

`run.sh` is the only entry point you need: it hands off to `install.py`, which detects the OS, installs dependencies the right way, downloads the AI models on first run, and starts the app. Every run after the first just starts.

### Windows — exe

Download `OrionsBelt.exe` from the [latest release](https://github.com/richard-callis/orions-belt/releases/latest), put it anywhere, double-click it. No Python, no admin rights, no separate install step — first launch downloads the AI models (~670 MB), every launch after opens straight to the app.

### Windows — from source

```cmd
run.bat
```

Same `install.py` under the hood as Linux/macOS, adapted for Windows. Double-click every time.

### Browser mode (headless / CI / no pywebview)

```bash
python launch.py       # falls back to opening http://localhost:5000 in your default browser
# or, to skip the desktop launcher entirely:
flask --app app run --host 127.0.0.1 --port 5000
```

On first launch, open **Settings** and configure an LLM provider before starting a chat.

## Project layout

```
orions-belt/
├── app/
│   ├── models/         # SQLAlchemy models — see docs/architecture.md#database
│   ├── routes/         # Flask blueprints — see docs/api-reference.md
│   ├── services/       # PII Guard, Memory, Agent Executor, MCP tool dispatch, connectors
│   ├── templates/      # Jinja2 + HTMX pages
│   └── static/         # Vendored CSS/JS (no runtime CDN dependency)
├── config.py            # App configuration
├── launch.py            # Desktop launcher (pywebview + pystray) and builtin-tool/Nova seeding
├── install.py           # Cross-platform installer/launcher (detects OS, installs, starts)
├── download_models.py   # Pre-download HuggingFace models
├── requirements.txt
└── tests/                # pytest suite
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system diagram, service internals, tool tiers, database schema
- [`docs/api-reference.md`](docs/api-reference.md) — core REST endpoints
- [`docs/development.md`](docs/development.md) — local dev workflow, configuration, extending the app, troubleshooting

## Relationship to ORION

```
ORION (Raspberry Pi cluster)         Orion's Belt (your laptop/desktop)
Manages the Kubernetes cluster  ↔    Manages your work
Provisions infrastructure            Projects, Epics, Tasks
GitOps / ArgoCD                      Local file + data operations
Remote control                       Local AI execution
```

Same design language (`#0f0f0f` / `#00A7E1`), different mission — ORION runs infrastructure, Orion's Belt runs your work.

## Security notes

**This is a local, single-user application.** It binds to `127.0.0.1` and is not designed for multi-user or network-exposed deployment.

- No authentication is enforced beyond localhost-only binding by default. Do not expose port 5000 externally.
- LLM API keys are stored in plaintext SQLite — set `SECRET_KEY` for session persistence and restrict `orions_belt.db` file permissions (`chmod 600`).
- Connector credentials are encrypted at rest with Fernet symmetric encryption; PII hashes use a configurable-salt SHA-256 and never leave the machine.
- File operations cannot escape the authorized-directory whitelist, symlinks are resolved before any comparison, and null-byte injection is blocked.
- Higher-risk tools (arbitrary shell execution, file deletion) require explicit human approval by design — see the tier system in [`docs/architecture.md`](docs/architecture.md).

---

*Named for the three stars of Orion's Belt: Alnitak · Alnilam · Mintaka — three pillars: Work · Agents · Connectors.*
