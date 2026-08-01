# API Reference

All endpoints are served on `http://127.0.0.1:5000` and require a valid session, except health checks and the first-run setup flow. Requests originating from `localhost` are auto-authenticated — see [Security Notes](../README.md#security-notes).

This page covers the core, stable endpoints most integrations need. The app has grown to 18 route blueprints (`app/routes/`) covering chat rooms, agents, connectors, MCP tools, Nova templates, memory, knowledge notes, digests, Dream review, PII exceptions, and more — for the complete and always-current list, read the blueprint files directly or open your browser's network tab against the running app.

## Health

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Service health + component status |

## Chat

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/sessions` | List recent sessions |
| `POST` | `/api/sessions` | Create a session |
| `PATCH` | `/api/sessions/<id>` | Rename a session |
| `DELETE` | `/api/sessions/<id>` | Delete a session and its messages |
| `GET` | `/api/sessions/<id>/messages` | Get message history |
| `POST` | `/api/sessions/<id>/stream` | Stream an LLM response (SSE) |

SSE event types from `/stream`:

```
event: text         data: {"delta": "..."}
event: tool_call    data: {"name": "read_file", "args": {...}}
event: tool_result  data: {"name": "read_file", "result": "..."}
event: done         data: {"total_tokens": 1234}
event: error        data: {"message": "..."}
```

## Settings

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/settings` | List all settings (API keys redacted) |
| `GET` | `/api/settings/<key>` | Get a single setting |
| `PUT` | `/api/settings/<key>` | Set a setting |

## Work (Projects → Epics → Features → Tasks)

| Method | Endpoint | Description |
|---|---|---|
| `GET`/`POST` | `/api/projects` | List / create projects |
| `GET`/`POST` | `/api/projects/<id>/epics` | List / create epics |
| `GET`/`POST` | `/api/epics/<id>/features` | List / create features |
| `GET`/`POST` | `/api/features/<id>/tasks` | List / create tasks |
| `PATCH` | `/api/tasks/<id>` | Update task status / assignment |

## Agents

| Method | Endpoint | Description |
|---|---|---|
| `GET`/`POST` | `/api/agents` | List / create agents |
| `PATCH`/`DELETE` | `/api/agents/<id>` | Update / delete an agent |
| `POST` | `/api/agents/<id>/run` | Start an agent run on a task |
| `GET` | `/api/agent-runs/<id>` | Get run status and steps |
| `POST` | `/api/agent-steps/<id>/approve` | Approve a Tier 3 pending step |

## Memory

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/memories` | List memories (`?type=`) |
| `POST` | `/api/memories` | Store a memory |
| `DELETE` | `/api/memories/<id>` | Delete a memory |
| `GET` | `/api/memories/search?q=<query>` | Semantic search via embedding similarity |
