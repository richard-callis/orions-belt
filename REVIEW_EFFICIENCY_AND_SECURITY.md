# Orion's Belt — Efficiency & Security Review

## Performance Bottlenecks

### P0 — High Impact

#### 1. ML Models Load on First Chat Request (30-60s freeze)
**Files:** `app/services/pii_guard/__init__.py`, `app/services/memory/__init__.py`

Both PII Guard and Memory Service use lazy initialization — the first user message triggers loading of ~670MB of ML models (spaCy, GLiNER, DeBERTa, SentenceTransformer) synchronously on the request thread. The user sees a 30-60 second hang on their first chat.

**Recommendation:** Start a background thread in `launch.py` after `db.create_all()` that calls `get_pii_guard()._ensure_initialized()` and `get_memory_service()._ensure_initialized()`. The models load while the user is still looking at the UI. First chat will work normally unless it arrives before loading finishes (in which case the existing lazy init picks up).

```python
# In launch.py, after _ensure_projects_dir():
def _preload_models():
    from app.services.pii_guard import get_pii_guard
    from app.services.memory import get_memory_service
    get_pii_guard()._ensure_initialized()
    get_memory_service()._ensure_initialized()

threading.Thread(target=_preload_models, daemon=True).start()
```

---

#### 2. New httpx.Client Per LLM Turn (repeated TLS handshakes)
**File:** `app/routes/chat.py` — `_stream_openai_gen`, lines 644, 727, 753

Each turn through the tool loop creates a fresh `httpx.Client(timeout=180.0)`, establishing a new TCP connection and TLS handshake to the LLM provider. With tool-calling flows that do 5-10 turns, this adds 500ms-2s of pure connection overhead.

Additionally, fallback1 and fallback2 each create their own client — that's up to 3 TLS handshakes per turn when the provider misbehaves.

**Recommendation:** Create a single `httpx.Client` before the while loop and reuse it for all turns. The client's connection pool will keep the TLS session alive across turns.

```python
with httpx.Client(timeout=180.0) as client:
    while turn_count < max_turns:
        # Use `client` for stream, fallback1, and fallback2
```

---

#### 3. N+1 Query in list_sessions
**File:** `app/routes/chat.py` — `list_sessions()`, line 133

`len(s.messages)` triggers SQLAlchemy to load *every* message for *every* session from the DB, just to count them. With 50 sessions of 30+ messages each, that's 1500+ message rows loaded into memory and immediately discarded.

**Recommendation:** Use a SQL `COUNT()` subquery or `db.session.query(func.count(...))` joined to sessions. This turns 51 queries into 1.

---

#### 4. asyncio.new_event_loop() Per Tool Call
**File:** `app/routes/chat.py` — `_run_tool()`, line 536

Every tool invocation creates and destroys a new asyncio event loop. Event loop creation is expensive (~1-3ms) and all the tool handlers are simple synchronous I/O wrapped in `async def` — they gain nothing from asyncio.

**Recommendation:** Either:
- (a) Remove `async` from all tool handlers in `app/services/mcp/tools.py` and call them directly (they do no actual async I/O except `_handle_call_connector` which uses `httpx.AsyncClient`), or
- (b) Create one event loop at module scope and reuse it for all tool calls

Option (a) is simpler and more honest — these handlers are synchronous code wearing an async costume. The only exception is `_handle_call_connector`'s REST branch which uses `httpx.AsyncClient` — switch it to the sync `httpx.Client` that's already imported.

---

#### 5. Excessive db.session.commit() Calls
**Files:** `app/services/pii_guard/__init__.py`, `app/routes/chat.py`, `app/services/mcp/tools.py`

- `_replace_with_tokens()` calls `db.session.commit()` inside a loop — once per PII span detected. A message with 5 PII entities does 5 commits.
- `_save_tool_message()` commits after each tool message.
- `_log_audit()` commits after each audit entry.
- `_save_user_message()` and `_save_assistant_message()` each commit.

Each commit forces an fsync to SQLite's WAL, which is ~5-10ms on spinning disk.

**Recommendation:** Batch commits. In `_replace_with_tokens`, move the commit outside the loop. In the tool execution loop, commit once after all tool results are saved. Consider flushing (not committing) during the loop and committing once at the end.

---

### P1 — Medium Impact

#### 6. Per-SSE-Line INFO Logging
**File:** `app/routes/chat.py`, line 664

`log.info("llm.raw[%d]: %s", ...)` fires on every SSE chunk from the provider. A typical response generates 50-200 log lines, each doing synchronous file I/O through the RotatingFileHandler.

**Recommendation:** Change `log.info` to `log.debug` for the raw line logging. The debug toggle already controls whether full content is shown — this just prevents the I/O when nobody's looking. Similarly for `llm.text_chunk` on line 690.

---

#### 7. Settings and Tool Definitions Rebuilt From DB Every Request
**File:** `app/routes/chat.py`, lines 371-414

Every chat request:
- Reads `llm.providers` from DB and JSON-parses it
- Reads `llm.active_provider` from DB
- Reads `pii.guard.enabled` from DB
- Queries all enabled MCPTools and parses their JSON schemas
- Reads `debug.llm` from DB

These values change rarely (only when the user edits settings) but are fetched on every single message.

**Recommendation:** Add a simple in-process cache with a short TTL (e.g., 5-10 seconds) to `Setting.get()`. A dict with `{key: (value, expiry_time)}` is sufficient. Invalidate on `Setting.set()`. Tool definitions could be cached similarly since they only change when tools are added/removed.

---

#### 8. Redundant Wrapper Generators
**File:** `app/routes/chat.py`, lines 1069-1082

`_stream_openai_with_tools` and `_stream_ollama` are thin wrappers that just re-yield from the actual generators. They're never called (the actual generators are called directly). Dead code.

**Recommendation:** Delete both wrapper functions.

---

### P2 — Lower Impact

#### 9. Memory Recall Brute-Forces All Embeddings
**File:** `app/services/memory/__init__.py` — `recall()`, line 178

`Memory.query.filter_by(pinned=False)` loads every memory from the DB, then computes cosine similarity in a Python loop. Fine for <100 memories, but scales linearly.

**Recommendation:** Not urgent — this will only matter if someone stores hundreds of memories. If it becomes an issue, consider SQLite's `sqlite-vss` extension or keep embeddings in a numpy array in memory (the current approach just needs to not reload from DB every time).

#### 10. Provider Config Parsed on Every Request
**File:** `app/routes/chat.py`, lines 371-388

`json.loads(llm_providers_raw)` runs on every chat request. The provider list is typically 1-3 entries and changes only when the user edits settings.

**Recommendation:** Covered by the Setting.get() cache in item 7.

---

## Security Concerns

### S1 — Critical

#### SQL Injection via Table Name in `_handle_call_connector`
**File:** `app/services/mcp/tools.py`, lines 530-534

```python
safe_table = action.replace("]", "")
cursor.execute(f"SELECT * FROM [{safe_table}]")
```

The `replace("]", "")` sanitization is incomplete. While the `[...]` bracket quoting in SQL Server prevents most injection, an attacker could craft a table name containing `]; DROP TABLE` with no closing bracket. The regex check on line 531 (`re.match(r"^[A-Za-z_][A-Za-z0-9_\.]*$", action)`) does catch this case — but only because `]` and `;` aren't in the allowed character class. The `.replace("]", "")` on line 533 is misleading dead code that suggests the regex might not be the real guard.

**Recommendation:** Remove the `.replace("]", "")` — the regex is the actual security boundary. The replace creates a false sense of defense-in-depth when it's actually unreachable.

---

#### Glob Pattern Injection in `_handle_search_files`
**File:** `app/services/mcp/tools.py`, line 255

```python
matches = list(Path(real_path).glob(f"**/{pattern}"))
```

The `pattern` parameter comes directly from LLM-generated tool arguments with no sanitization. While `Path.glob()` doesn't execute commands, patterns like `../../**/*` combined with the `**/` prefix could traverse above the authorized directory. The `_authorize_path` check on line 252 only validates the root `path`, not the matched results.

**Recommendation:** Validate that each matched file is within the authorized directory (call `_authorize_path` on matched results), or sanitize the pattern to reject `..` components.

---

### S2 — Medium

#### No Rate Limiting on Chat Endpoint
**File:** `app/routes/chat.py`, line 328

The `/api/sessions/<id>/stream` endpoint has no rate limiting. While this is a localhost app, if it's ever exposed on a network (or a malicious browser tab discovers it), an attacker could flood it with requests, each spawning expensive ML inference (PII guard) and outbound API calls.

**Recommendation:** Add a simple in-memory rate limiter (e.g., max 5 concurrent streams, max 30 requests/minute per session). Even a threading.Semaphore would help.

---

#### Tool Result Size Unbounded in LLM Context
**File:** `app/routes/chat.py`, lines 831-856

Tool results are appended to the `messages` list with `str(result)` — no size cap. A `read_file` with `max_bytes=1048576` (1MB) returns up to 1MB of content that gets stuffed into the LLM context. This could:
1. Blow past the provider's context limit causing API errors
2. Cost significant tokens on pay-per-token providers

**Recommendation:** Truncate tool results before adding to the LLM context (the truncation in `truncate_history` from `app/services/llm.py` is never applied to tool results in the agentic loop — it's only used on the initial history load).

---

#### PII Hash Token Collision Risk
**File:** `app/services/pii_guard/__init__.py`, line 429

```python
token = full_hash[:8]  # short token shown inline
```

8 hex characters = 32 bits of entropy. With the birthday paradox, collisions become likely around ~65,000 unique PII values. Two different PII values mapping to the same token would cause `restore()` to replace one person's data with another's.

**Recommendation:** Increase to 12 or 16 hex characters, or detect collisions during insertion and extend the token if a collision is found.

---

#### Connector REST Call is Server-Side Request (SSRF potential)
**File:** `app/services/mcp/tools.py`, lines 509-514

```python
url = config.get("url", "") + "/" + action
resp = await client.request(method, url, json=params)
```

The `action` parameter from the LLM is appended to the connector URL. An LLM could craft an `action` like `../../internal-api/admin` to hit unintended endpoints. The URL is constructed via string concatenation, not URL joining, so path traversal in `action` would work.

**Recommendation:** Use `urllib.parse.urljoin` and validate the final URL's host matches the configured connector URL's host. Or restrict `action` to simple path segments (no `/`, `..`, or query strings).

---

### S3 — Low / Informational

#### SQLite WAL Mode Not Explicitly Set
**File:** `config.py`

SQLite defaults to journal mode, not WAL. With `threaded=True` in Flask, concurrent reads and writes will contend. WAL mode allows concurrent readers during writes.

**Recommendation:** Add `SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 15}, "pool_pre_ping": True}` and execute `PRAGMA journal_mode=WAL` on connect.

---

#### Secret Key File Permissions
**File:** `config.py`, line 33

`key_file.chmod(0o600)` is correct on Linux but has no effect on Windows (NTFS permissions work differently). On a shared Windows machine, any user could read `.secret_key`.

**Recommendation:** On Windows, use `icacls` or the `win32security` module to restrict access. Or store the key in Windows Credential Manager via `keyring`.

---

#### Message Content Truncated at 4000 Chars
**File:** `app/routes/chat.py`, lines 1027, 1042, 1058

`content[:4000]` silently drops content beyond 4000 characters. If the LLM produces a long response, the truncation is invisible to the user — the DB stores less than what was displayed. On reload, the conversation will appear to have lost content.

**Recommendation:** Either don't truncate (use `db.Text` which has no limit in SQLite), or show a visual indicator in the UI when truncation occurred. The `db.Column(db.Text)` on the model already supports unlimited length — the truncation is artificial.

---

## Summary

| Priority | Issue | Est. Impact |
|----------|-------|-------------|
| P0 | Background-load ML models at startup | -30-60s first-request latency |
| P0 | Reuse httpx.Client across turns | -500ms-2s per tool-calling conversation |
| P0 | Fix N+1 in list_sessions | -200ms+ on sessions list with 50 sessions |
| P0 | Remove async wrappers from sync tool handlers | -1-3ms per tool call, simpler code |
| P0 | Batch DB commits in PII guard + tool loop | -50-100ms per message with PII |
| P1 | Downgrade per-line log to DEBUG | Reduced disk I/O during streaming |
| P1 | Cache Settings + tool defs with short TTL | -5-10ms per request (5+ DB queries saved) |
| P2 | Delete dead wrapper generators | Dead code cleanup |
| S1 | Validate glob results against authorized paths | Prevent path traversal via search_files |
| S2 | Truncate tool results in LLM context | Prevent context overflow + cost blowup |
| S2 | Increase PII hash token length | Prevent collision at scale |
| S2 | Sanitize REST connector action parameter | Prevent SSRF via LLM-crafted tool args |
