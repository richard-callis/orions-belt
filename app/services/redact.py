"""
Secret redaction for logging.

execute_tool() logs every tool call's args and result preview — the single
busiest logging chokepoint in the app (all ~44 tools, every call). Until
now that logging only truncated long values, which doesn't protect a
short-to-medium secret from ending up in a log file someone shares in a
bug report or screen-share. This is deliberately NOT about tool-call args
containing connector credentials directly (those live server-side in
Fernet-encrypted Connector.auth_config, referenced by connector name, not
passed as LLM-supplied args) — the real exposure is tool RESULTS:
read_file/fetch_url/search_documents/run_shell can return the literal
contents of a .env file, an API response with an Authorization header
echoed back, or a git diff touching a secrets file, and that flows
straight into the result-preview log line.

Two layers, matching the shape of the actual risk:
  - Field-name-aware: a dict value under a key like "password"/"token"/
    "api_key" is masked outright, regardless of whether it happens to look
    like a secret — the field name alone is the signal.
  - Pattern-based: free text (tool results, file contents, HTTP responses)
    is scanned for known secret SHAPES (Bearer tokens, JWTs, GitHub/OpenAI/
    Slack/AWS-style prefixed tokens, Fernet tokens, PEM private key blocks)
    since there's no field name to key off there.
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Field names (case-insensitive, exact match after stripping non-alnum) whose
# value is always masked regardless of shape — a dict key literally named
# "password" is reason enough on its own.
_SENSITIVE_FIELD_NAMES = {
    "password", "passwd", "pwd",
    "secret", "clientsecret", "client_secret",
    "token", "accesstoken", "access_token", "refreshtoken", "refresh_token",
    "apikey", "api_key", "apitoken", "api_token",
    "privatekey", "private_key",
    "authorization", "auth", "bearer",
    "credential", "credentials", "pat",
}


def _normalize_field_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Pattern-based detectors for secret SHAPES in free text — each is a
# (name, compiled regex) pair; matches are replaced with a labeled
# placeholder so it's still obvious *something* was redacted and roughly
# what kind, without leaking any of the actual value.
_SECRET_PATTERNS = [
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.=]{10,}", re.IGNORECASE)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    # Allows hyphens/underscores within the key body (not just alnum) so the
    # newer project-scoped format (sk-proj-...) matches too — the old
    # alnum-only pattern stopped at the first hyphen after "sk-" and missed
    # it entirely.
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("fernet_token", re.compile(r"\bgAAAAA[A-Za-z0-9_=-]{20,}\b")),
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    )),
]


def redact_text(text: str) -> str:
    """Scan free text for known secret shapes and mask each match. Safe to
    call on arbitrary/non-secret text — only replaces what actually matches
    a pattern above, everything else passes through unchanged."""
    if not text:
        return text
    for name, pattern in _SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return text


def redact_value(key: str, value):
    """Redact a single (key, value) pair for logging: masked outright if
    the key name is sensitive, otherwise pattern-scanned if it's a string,
    otherwise passed through unchanged (numbers/bools/None/nested
    structures aren't secrets on their own)."""
    if _normalize_field_name(str(key)) in _SENSITIVE_FIELD_NAMES:
        return _REDACTED
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_args(args: dict) -> dict:
    """Redact a flat args dict for logging — one pass, top-level keys only
    (matches how execute_tool's args dicts are shaped; nested structures
    are rare enough here that a shallow pass covers the real risk without
    the complexity of a recursive walk)."""
    if not isinstance(args, dict):
        return args
    return {k: redact_value(k, v) for k, v in args.items()}


def redact_deep(obj):
    """Recursively redact a JSON-shaped structure (dicts/lists/strings) at
    every nesting level — both layers combined, unlike redact_args above.

    Built for the LLM traffic-capture feature: a captured request/response
    body is arbitrarily nested (message content, tool call args, provider-
    specific wrapper fields), so a shallow top-level-only pass like
    redact_args would miss a "api_key" or "authorization" field sitting
    inside a nested dict — exactly the shape a provider payload has. Dict
    keys are checked against the sensitive-field-name list at every level;
    string values (including ones under non-sensitive keys) are still
    pattern-scanned by redact_text so a secret embedded in ordinary text
    (e.g. an echoed header, a pasted token) gets caught too.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _normalize_field_name(str(k)) in _SENSITIVE_FIELD_NAMES:
                out[k] = _REDACTED
            else:
                out[k] = redact_deep(v)
        return out
    if isinstance(obj, list):
        return [redact_deep(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_deep(v) for v in obj)
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
