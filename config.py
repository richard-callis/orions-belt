import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ── HuggingFace model cache — store inside project/models/ ────
# Models download once on first use, then served from cache.
# Delete the models/ directory to force a re-download.
os.environ.setdefault("HF_HOME", str(BASE_DIR / "models"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(BASE_DIR / "models" / "hub"))


def _load_or_create_secret_key() -> str:
    """Load SECRET_KEY from env or a local .secret_key file.

    The key is generated once on first run and persisted across restarts.
    Priority: SECRET_KEY env var > .secret_key file > generate new.
    The .secret_key file is listed in .gitignore and has mode 0o600.
    """
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_file = BASE_DIR / ".secret_key"
    if key_file.exists():
        stored = key_file.read_text().strip()
        if stored:
            return stored
    new_key = secrets.token_hex(32)
    try:
        key_file.write_text(new_key)
        key_file.chmod(0o600)
    except OSError:
        pass  # read-only filesystem — key won't persist, that's acceptable
    return new_key


class Config:
    # ── Database ──────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'orions_belt.db'}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Security ──────────────────────────────────────────────
    # Secret key is persisted to .secret_key (gitignored) so sessions survive
    # app restarts. Override with SECRET_KEY env var for production deploys.
    SECRET_KEY = _load_or_create_secret_key()

    # ── LLM Provider (set via UI on first run) ────────────────
    LLM_BASE_URL = "https://api.openai.com/v1"
    LLM_API_KEY = ""
    LLM_MODEL = "gpt-4o"
    LLM_MAX_TOKENS = 4096

    # ── PII Guard — transformers-based pipeline ───────────────
    # Stage 1: Presidio rule-based (SSN, email, phone, credit card...)
    # Stage 2: dslim/bert-base-NER — contextual NER (names, orgs, locations)
    # Stage 3: cross-encoder/nli-deberta-v3-small — zero-shot PHI classifier
    # All models downloaded automatically from HuggingFace on first use.
    # No GGUF, no llama.cpp, no compiler required — pure pip.
    PII_NER_MODEL = "dslim/bert-base-NER"
    PII_JUDGE_MODEL = "cross-encoder/nli-deberta-v3-small"
    PII_JUDGE_THRESHOLD = 0.75       # confidence threshold to flag as PII/PHI
    PII_HASH_SALT = os.environ.get("PII_HASH_SALT", "orions-belt-pii")

    # ── Context window management ─────────────────────────────
    CONTEXT_TOKEN_WARN = 0.75        # warn at 75% of model max
    CONTEXT_TOKEN_COMPACT = 0.90     # auto-compact at 90%
    CONTEXT_SLIDING_WINDOW = 20      # messages kept in sliding mode

    # ── Projects root directory ───────────────────────────────
    # Each project gets a subfolder here when created.
    # Override with PROJECTS_DIR env var to point at a different location.
    PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", str(BASE_DIR / "projects")))

    # ── MCP / File operations ─────────────────────────────────
    # Tier 2 countdown before auto-proceeding (seconds)
    MCP_WARN_COUNTDOWN = 10
    # Always-blocked paths (Windows system dirs)
    MCP_BLOCKED_PATHS = [
        "C:\\Windows",
        "C:\\Program Files",
        "C:\\Program Files (x86)",
        "C:\\ProgramData\\Microsoft",
    ]

    # ── Logging ───────────────────────────────────────────────
    LOG_DIR = BASE_DIR / "logs"
    LOG_ROTATION_MB = 10
    LOG_RETENTION_DAYS = 30

    # ── Memory / embeddings ───────────────────────────────────
    MEMORY_EMBEDDING_MODEL = "all-MiniLM-L6-v2"   # small, fast, CPU
    MEMORY_TOP_K = 5                               # memories injected per call

    # ── UI ────────────────────────────────────────────────────
    APP_NAME = "Orion's Belt"
    APP_VERSION = "0.1.0"
    WINDOW_WIDTH = 1400
    WINDOW_HEIGHT = 900
    WINDOW_MIN_WIDTH = 1024
    WINDOW_MIN_HEIGHT = 600
