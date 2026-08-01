# Development

## Local dev loop

```bash
# Hot reload + debug errors in browser
export FLASK_DEBUG=1
flask --app app run --host 127.0.0.1 --port 5000

# Pre-download all HuggingFace models (avoids first-run delay)
python download_models.py

# Reset the database (start fresh, all data lost)
rm orions_belt.db
python launch.py

# Re-generate the tray icon
python create_icon.py

# Run the test suite
python -m pytest -q
```

## Configuration

### LLM providers

Configure in **Settings → LLM Providers**. Any OpenAI-compatible endpoint works:

| Provider | Base URL | Notes |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | Requires API key |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/deployments/<deployment>` | API key = Azure key |
| Ollama (local) | `http://localhost:11434/v1` | No API key needed |
| LM Studio | `http://localhost:1234/v1` | No API key needed |
| llama-server | `http://localhost:8080/v1` | No API key needed |
| Any OpenAI-compatible | Custom URL | Set model name accordingly |

Multiple providers can be saved and switched between at any time.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | Random, regenerated each restart | Flask session key — **set this** or sessions break on restart |
| `PII_HASH_SALT` | `orions-belt-pii` | Salt for PII SHA-256 hashing — change before first run to customize |
| `LLM_TIMEOUT` | `600` (seconds) | Per-request timeout for LLM API calls |
| `PROJECTS_DIR` | `./projects` | Where project-scoped file output is written |
| `HF_HOME` | `./models` | HuggingFace model cache directory |
| `TRANSFORMERS_CACHE` | `./models/hub` | Transformers-specific cache path |

Create a `.env` file in the project root (already `.gitignore`d):

```bash
SECRET_KEY=your-32-char-hex-secret-here
PII_HASH_SALT=your-custom-salt-here
```

### Authorized directories

File-touching tools require explicit authorization: **Settings → Authorized Directories → Add Directory**.

- **Alias** — the name the LLM sees (e.g. `project_files`), not the real path
- **Path** — the real path on disk
- **Read-only** — blocks every write regardless of a tool's own tier
- **Max tier** — caps the highest tier of operation allowed in that directory
- **Expiration** — optional, for temporary grants

## Extending the app

### Add a new MCP tool

1. Add the tool definition to `_seed_builtin_tools()` in `launch.py` (name, tier, JSON schema).
2. Add a `_handle_<tool_name>` async function in `app/services/mcp/tools.py`.
3. Register it in the `handlers` dict inside `execute_tool()` in the same file.
4. Pick the tier deliberately — see the tier table in [Architecture](architecture.md#mcp-tool-executor--appservicesmcptoolspy). If the tool touches a path argument, add that argument's key to `path_args` in `execute_tool()` so per-directory `read_only`/`max_tier` caps apply to it.

### Add a new LLM provider

Any OpenAI-compatible API works out of the box — **Settings → LLM Providers → Add Provider**, base URL and model name are all that's required (API key optional for local models).

### Add a new connector type

See `app/services/oauth_providers.py` for the OAuth-based connectors (Google, Microsoft Graph, Salesforce) and `app/routes/connectors.py` for the full set of supported `connector_type` values.

## Troubleshooting

**Flask won't start / port conflict**
```bash
lsof -i :5000                    # Linux/macOS
netstat -ano | findstr :5000     # Windows
```

**HuggingFace models fail to download**
```bash
python download_models.py            # run directly for verbose output
# behind a proxy:
python download_models.py --ssl-bypass
```

**spaCy model missing**
```bash
python -m spacy download en_core_web_sm
```

**Database corruption / migration issues**
```bash
rm orions_belt.db   # fresh start — all data lost
python launch.py
```

**pywebview won't open (Linux)**
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.0
# or just let it fall back to browser mode automatically:
python launch.py
```

**Windows: pywin32 import error**
```cmd
pip install pywin32
python -m win32com.client.makepy
```
