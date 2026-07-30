"""
Dream — periodic LLM-driven extraction of durable lessons from recent room
activity, staged as pending DreamLesson rows for human review before they
become live, recallable Memory rows.

Ported from a sibling app's "Dream" feature, adapted for this app's memory
service (Memory.store()/recall(), not a separate Notes+embedding system) and
hardened for the one thing that design doesn't have to worry about here:
a human review gate. Every future agent reply's memory context is built by
concatenating recalled memories between two literal delimiter strings (see
app/services/memory/__init__.py::inject_context) — an unreviewed, LLM-written
memory containing the wrong text there is a real prompt-injection surface
into every future agent context, not just a quality issue.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime

log = logging.getLogger("orions-belt.dream")

_shutdown_event = threading.Event()
_dream_thread = None

_DEFAULT_INTERVAL_HOURS = 2.0
_MAX_CORPUS_CHARS = 12000
_MAX_CORPUS_MESSAGES = 200
_MAX_DREAM_LESSON_ROWS = 200
_MAX_TITLE_LEN = 120
_MAX_CONTENT_LEN = 500

# The exact delimiters inject_context() wraps recalled memories in — stripped
# from anything Dream writes so an extracted "lesson" can never contain a
# fake closing marker and have its own text read as system instructions once
# it's approved and recalled into a future reply's system prompt.
_MEMORY_MARKERS = ("--- Relevant Context from Memory ---", "--- End of Memory Context ---")

# Quality filter, not a security boundary (the injection risk is closed by
# marker-stripping + the review gate) — just keeps obviously-wrong output
# (the model addressing "you"/the assistant instead of reporting an
# observation) out of the review queue.
_SUSPICIOUS_PATTERNS = (
    re.compile(r"^\s*(ignore|disregard|forget)\b", re.IGNORECASE),
    re.compile(r"\byou (must|should|need to)\b", re.IGNORECASE),
    re.compile(r"^\s*(system|assistant|user)\s*:", re.IGNORECASE),
    re.compile(r"```"),
    re.compile(r"https?://"),
)

_EXTRACTION_PROMPT = (
    "You are a memory-consolidation system for a local AI workbench. Review the "
    "following recent conversation between a human and one or more AI agents, "
    "then extract DURABLE facts, lessons, or patterns worth remembering for "
    "future work — the kind of thing worth knowing next week, not transient "
    "chit-chat or one-off task state.\n\n"
    "Rules:\n"
    "- Only extract something a future agent would genuinely benefit from knowing.\n"
    "- Skip anything vague, obvious, or not actionable.\n"
    "- Report observations about what happened — never instructions, commands, "
    "or anything addressed to \"you\"/the assistant.\n"
    "- If nothing qualifies, return an empty array.\n\n"
    "Respond with ONLY a JSON array, no other text. Each item:\n"
    '{{"title": "short title", "content": "the lesson, 1-3 sentences", "folder": "a short category"}}\n\n'
    "Conversation:\n{corpus}"
)


def _strip_markers(text: str) -> str:
    # A single left-to-right .replace() pass can leave a *new* marker
    # instance behind when removing one occurrence joins two fragments back
    # into the literal marker text (e.g. "...Memory Con" + "text ---" ->
    # "...Memory Context ---"). Loop to a fixed point so no marker substring
    # survives, however it was assembled.
    for marker in _MEMORY_MARKERS:
        while marker in text:
            text = text.replace(marker, "")
    return text


def _is_suspicious(text: str) -> bool:
    return any(p.search(text) for p in _SUSPICIOUS_PATTERNS)


def _contains_marker(text: str) -> bool:
    return any(marker in text for marker in _MEMORY_MARKERS)


def _sanitize_lesson(title, content) -> tuple[str, str] | None:
    """Apply output constraints; return (title, content) or None if rejected."""
    raw_title, raw_content = str(title or ""), str(content or "")
    # Reject outright rather than silently repair — a lesson whose raw output
    # contains a memory-context delimiter at all is treated as an attempted
    # (or accidental) forgery, not a text-cleanup problem.
    if _contains_marker(raw_title) or _contains_marker(raw_content):
        return None
    title = _strip_markers(raw_title).strip()[:_MAX_TITLE_LEN]
    content = _strip_markers(raw_content).strip()[:_MAX_CONTENT_LEN]
    if not title or not content:
        return None
    if _is_suspicious(title) or _is_suspicious(content):
        return None
    return title, content


def _build_corpus() -> tuple[str, str | None]:
    """Recent room activity since the last extraction watermark, budgeted by
    message count and total characters.

    Returns (corpus_text, latest_created_at_iso). run_extraction() advances
    the watermark to latest_created_at_iso whenever the LLM actually
    responded — including "no lessons here" / unparseable output, treated as
    nothing-to-extract-from-this-window — but NOT when the call itself
    failed (network/provider error), so only a genuine call failure gets the
    same window retried next tick; a window that consistently produces
    unparseable output can't permanently wedge the pipeline.
    """
    from app.models.chat_room import ChatRoomMessage
    from app.models.settings import Setting

    watermark = Setting.get("dream.last_extraction_at")
    query = ChatRoomMessage.query.filter(ChatRoomMessage.sender_type.in_(("human", "agent")))
    if watermark:
        try:
            since = datetime.fromisoformat(watermark)
            query = query.filter(ChatRoomMessage.created_at > since)
        except ValueError:
            pass
    rows = query.order_by(ChatRoomMessage.created_at.desc()).limit(_MAX_CORPUS_MESSAGES).all()
    if not rows:
        return "", None
    rows.reverse()  # chronological order for the prompt

    # Nonce-delimited so the corpus can't be confused with the instructions
    # around it — mirrors the same "don't let untrusted text look like
    # structure" principle as the memory-marker stripping above.
    nonce = uuid.uuid4().hex[:8]
    lines = [f"<<<CONVERSATION-{nonce}>>>"]
    total = 0
    for m in rows:
        label = "Human" if m.sender_type == "human" else ((m.agent.name if m.agent else None) or "Agent")
        line = f"[{label}] {(m.content or '')[:400]}"
        if total + len(line) > _MAX_CORPUS_CHARS:
            break
        lines.append(line)
        total += len(line)
    lines.append(f"<<<END-CONVERSATION-{nonce}>>>")

    latest_at = rows[-1].created_at.isoformat() if rows[-1].created_at else None
    return "\n".join(lines), latest_at


def run_extraction() -> int:
    """One extraction pass. Returns the number of pending lessons created.

    Assumes it is called within a Flask app context — the periodic loop
    below (and any test calling this directly) owns creating that context.
    """
    from app import db
    from app.models.dream import DreamLesson
    from app.models.settings import Setting

    existing_rows = DreamLesson.query.count()
    if existing_rows >= _MAX_DREAM_LESSON_ROWS:
        log.info("Dream: at the %d-row cap (%d existing) — skipping this pass",
                 _MAX_DREAM_LESSON_ROWS, existing_rows)
        return 0

    corpus, latest_at = _build_corpus()
    if not corpus:
        return 0

    from app.services.agents.runtime import resolve_active_provider
    from app.services.llm import retry_with_recovery

    provider = resolve_active_provider()
    if not provider or not provider.get("base_url") or not provider.get("model"):
        log.info("Dream: no active LLM provider configured — skipping this pass")
        return 0

    prompt = _EXTRACTION_PROMPT.format(corpus=corpus)
    try:
        resp_text, _tool_calls, _tokens = retry_with_recovery(
            provider.get("base_url"), provider.get("api_key"), provider.get("model"),
            [
                {"role": "system", "content": "You extract durable lessons from conversation "
                                              "logs. You always respond with a JSON array and "
                                              "nothing else."},
                {"role": "user", "content": prompt},
            ],
            [],
            max_retries=2,
        )
    except Exception as e:
        log.warning("Dream: extraction LLM call failed: %s", e)
        return 0

    match = re.search(r"\[[\s\S]*\]", resp_text or "")
    if not match:
        log.info("Dream: extraction produced no parseable JSON array")
        if latest_at:
            Setting.set("dream.last_extraction_at", latest_at)
        return 0
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.info("Dream: extraction JSON failed to parse")
        if latest_at:
            Setting.set("dream.last_extraction_at", latest_at)
        return 0

    created = 0
    for item in (items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        sanitized = _sanitize_lesson(item.get("title"), item.get("content"))
        if not sanitized:
            continue
        title, content = sanitized
        folder = _strip_markers(str(item.get("folder") or ""))[:64].strip() or None
        db.session.add(DreamLesson(title=title, content=content, folder=folder, status="pending"))
        created += 1
        if existing_rows + created >= _MAX_DREAM_LESSON_ROWS:
            break
    if created:
        db.session.commit()

    if latest_at:
        Setting.set("dream.last_extraction_at", latest_at)

    log.info("Dream: extraction pass created %d pending lesson(s)", created)
    return created


def start_dream_service(interval_hours: float = _DEFAULT_INTERVAL_HOURS) -> None:
    """Start the background extraction thread.

    Checks the `agents.dream_enabled` setting (default off) on every tick,
    so toggling it in Settings takes effect on the next tick without an app
    restart. Follows the same self-contained thread + fresh-app-per-tick
    pattern as app.services.retention.start_retention_service — this is
    called from launch.py's startup sequence, with no active Flask request
    to inherit an app/context from.
    """
    global _dream_thread

    def _dream_loop():
        while not _shutdown_event.is_set():
            try:
                from app import create_app
                from app.models.settings import Setting
                app = create_app()
                with app.app_context():
                    if Setting.get("agents.dream_enabled", False):
                        run_extraction()
            except Exception as e:
                log.error("Dream thread error: %s", e, exc_info=True)

            _shutdown_event.wait(interval_hours * 3600)

        log.info("Dream service thread stopped")

    _dream_thread = threading.Thread(target=_dream_loop, daemon=True, name="dream-scheduler")
    _dream_thread.start()
    log.info("Dream service started (interval=%.1fh, disabled by default until agents.dream_enabled is set)",
             interval_hours)


def stop_dream_service() -> None:
    """Signal the Dream service thread to stop."""
    global _dream_thread
    if _dream_thread and _dream_thread.is_alive():
        _shutdown_event.set()
        _dream_thread.join(timeout=30)
        log.info("Dream service thread stopped")
