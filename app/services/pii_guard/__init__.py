"""
Orion's Belt — PII Guard Service
Three-stage PII/PHI detection pipeline. All detection runs locally on CPU.
No PII ever leaves the machine — detected values are hashed and stored locally.

Pipeline:
  Stage 1: Presidio (rule-based) — SSN, email, phone, credit card, passport
  Stage 2: BERT NER (dslim/bert-base-NER) — contextual PERSON/ORG/LOC detection
  Stage 3: DeBERTa zero-shot judge — PHI classification of ambiguous spans

Graceful degradation:
  - If transformers models fail to load → Presidio-only mode
  - If Presidio fails → pass text through unchanged (logged as warning)

Usage:
    guard = get_pii_guard()
    clean_text, pii_found, entity_types = guard.scan(text, session_id="abc")
    original = guard.restore(clean_text)
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from typing import Optional

log = logging.getLogger("orions-belt.pii_guard")

_instance: Optional["PIIGuard"] = None
_lock = threading.Lock()


def get_pii_guard() -> "PIIGuard":
    """Return the singleton PIIGuard instance (created on first call)."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = PIIGuard()
    return _instance


class PIIGuard:
    """Three-stage PII/PHI detection and hashing pipeline."""

    def __init__(self):
        self._presidio_ready = False
        self._ner_ready = False
        self._judge_ready = False
        self._presidio_analyzer = None
        self._ner_pipeline = None
        self._judge_pipeline = None
        self._init_lock = threading.Lock()
        self._initialized = False

    # ── Lazy initialization ───────────────────────────────────────────────────

    def _ensure_initialized(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._init_presidio()
            self._init_ner()
            self._init_judge()
            self._initialized = True

    def _init_presidio(self):
        try:
            from presidio_analyzer import AnalyzerEngine
            self._presidio_analyzer = AnalyzerEngine()
            self._presidio_ready = True
            log.info("PII Guard: Presidio analyzer loaded")
        except Exception as e:
            log.warning(f"PII Guard: Presidio unavailable ({e}) — Stage 1 disabled")

    def _init_ner(self):
        try:
            from transformers import pipeline as hf_pipeline
            from config import Config
            model_name = getattr(Config, "PII_NER_MODEL", "dslim/bert-base-NER")
            self._ner_pipeline = hf_pipeline(
                "ner",
                model=model_name,
                aggregation_strategy="simple",
                device=-1,  # CPU only
            )
            self._ner_ready = True
            log.info(f"PII Guard: NER pipeline loaded ({model_name})")
        except Exception as e:
            log.warning(f"PII Guard: NER model unavailable ({e}) — Stage 2 disabled")

    def _init_judge(self):
        try:
            from transformers import pipeline as hf_pipeline
            from config import Config
            model_name = getattr(Config, "PII_JUDGE_MODEL", "cross-encoder/nli-deberta-v3-small")
            self._judge_pipeline = hf_pipeline(
                "zero-shot-classification",
                model=model_name,
                device=-1,
            )
            self._judge_ready = True
            log.info(f"PII Guard: Judge pipeline loaded ({model_name})")
        except Exception as e:
            log.warning(f"PII Guard: Judge model unavailable ({e}) — Stage 3 disabled")

    # ── Core API ──────────────────────────────────────────────────────────────

    def scan(
        self,
        text: str,
        session_id: str | None = None,
        message_id: str | None = None,
        direction: str = "outbound",
    ) -> tuple[str, bool, list[str]]:
        """Scan text for PII/PHI and replace detected values with hash tokens.

        Returns:
            (cleaned_text, pii_detected: bool, entity_types: list[str])

        The cleaned_text contains [PII:TYPE:hash_token] placeholders.
        The hash → original mapping is stored in the local DB.
        """
        if not text or not text.strip():
            return text, False, []

        self._ensure_initialized()

        # Collect spans: list of (start, end, entity_type, original, source)
        spans: list[tuple[int, int, str, str, str]] = []

        # Stage 1: Presidio
        if self._presidio_ready:
            try:
                results = self._presidio_analyzer.analyze(text=text, language="en")
                for r in results:
                    spans.append((r.start, r.end, r.entity_type, text[r.start:r.end], "presidio"))
            except Exception as e:
                log.debug(f"PII Guard: Presidio scan error: {e}")

        # Stage 2: BERT NER
        if self._ner_ready:
            try:
                ner_results = self._ner_pipeline(text)
                for entity in ner_results:
                    label = entity.get("entity_group", entity.get("entity", "MISC"))
                    word = entity.get("word", "")
                    start = entity.get("start", 0)
                    end = entity.get("end", len(word))
                    if label in ("PER", "PERSON", "ORG", "LOC", "GPE"):
                        # Normalize label
                        normalized = {"PER": "PERSON", "GPE": "LOCATION", "LOC": "LOCATION"}.get(label, label)
                        spans.append((start, end, normalized, word, "ner"))
            except Exception as e:
                log.debug(f"PII Guard: NER scan error: {e}")

        # Deduplicate overlapping spans (keep highest-confidence, longest span)
        spans = _deduplicate_spans(spans)

        # Stage 3: DeBERTa judge on any remaining ambiguous NER spans
        if self._judge_ready and spans:
            try:
                judge_threshold = 0.75
                try:
                    from config import Config
                    judge_threshold = getattr(Config, "PII_JUDGE_THRESHOLD", 0.75)
                except Exception:
                    pass

                verified_spans = []
                for span in spans:
                    start, end, etype, value, source = span
                    if source == "ner" and len(value) > 2:
                        result = self._judge_pipeline(
                            value,
                            candidate_labels=["personal information", "medical information", "general text"],
                        )
                        top_label = result["labels"][0]
                        top_score = result["scores"][0]
                        if top_label in ("personal information", "medical information") and top_score >= judge_threshold:
                            verified_spans.append(span)
                        else:
                            log.debug(f"PII Guard: Judge dismissed '{value}' as '{top_label}' ({top_score:.2f})")
                    else:
                        verified_spans.append(span)
                spans = verified_spans
            except Exception as e:
                log.debug(f"PII Guard: Judge error: {e}")

        if not spans:
            return text, False, []

        # Replace spans with hash tokens (working from end to preserve positions)
        entity_types = list({s[2] for s in spans})
        clean_text = _replace_with_tokens(text, spans, session_id, message_id)

        # Log detection event
        try:
            _log_pii_detection(
                session_id=session_id,
                message_id=message_id,
                direction=direction,
                entity_types=entity_types,
                count=len(spans),
                sources=list({s[4] for s in spans}),
            )
        except Exception as e:
            log.debug(f"PII Guard: logging error: {e}")

        return clean_text, True, entity_types

    def restore(self, text: str) -> str:
        """Replace [PII:TYPE:hash_token] markers with original values from DB.

        Used to show the user the restored (plain) text after an LLM response
        comes back with hash tokens in it. Only reads from local DB.
        """
        if "[PII:" not in text:
            return text

        try:
            from app.models.pii import PIIHashEntry
            tokens = re.findall(r"\[PII:[A-Z_]+:([a-f0-9]+)\]", text)
            for token in tokens:
                entry = PIIHashEntry.query.filter_by(hash_token=token).first()
                if entry:
                    placeholder = f"[PII:{entry.entity_type}:{token}]"
                    text = text.replace(placeholder, entry.original_value)
        except Exception as e:
            log.debug(f"PII Guard: restore error: {e}")

        return text

    @property
    def status(self) -> str:
        """Return current operational status: ready | degraded | disabled."""
        if not self._initialized:
            return "not_loaded"
        if self._presidio_ready:
            if self._ner_ready and self._judge_ready:
                return "ready"
            return "degraded"
        return "disabled"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _deduplicate_spans(spans: list) -> list:
    """Remove overlapping spans, keeping longer ones."""
    if not spans:
        return []
    spans = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))
    result = []
    last_end = -1
    for span in spans:
        if span[0] >= last_end:
            result.append(span)
            last_end = span[1]
    return result


def _make_hash_token(value: str, entity_type: str) -> tuple[str, str]:
    """Return (full_sha256_hex, short_token) for a PII value."""
    try:
        from config import Config
        salt = getattr(Config, "PII_HASH_SALT", "orions-belt-pii")
    except Exception:
        salt = "orions-belt-pii"
    raw = f"{salt}:{entity_type}:{value}"
    full_hash = hashlib.sha256(raw.encode()).hexdigest()
    token = full_hash[:8]  # short token shown inline
    return full_hash, token


def _replace_with_tokens(
    text: str,
    spans: list,
    session_id: str | None,
    message_id: str | None,
) -> str:
    """Replace all detected spans with [PII:TYPE:hash_token] markers.

    Works from end of string to preserve character positions.
    Persists hash → original mappings to the local DB.
    """
    from app import db
    from app.models.pii import PIIHashEntry

    spans_sorted = sorted(spans, key=lambda s: s[0], reverse=True)
    result = text

    for start, end, entity_type, original, source in spans_sorted:
        full_hash, token = _make_hash_token(original, entity_type)

        # Upsert: only create if this token doesn't already exist
        try:
            entry = PIIHashEntry.query.filter_by(hash_token=token).first()
            if not entry:
                entry = PIIHashEntry(
                    id=str(uuid.uuid4()),
                    hash_token=token,
                    full_hash=full_hash,
                    original_value=original,
                    entity_type=entity_type,
                    detection_source=source,
                    session_id=session_id,
                    message_id=message_id,
                )
                db.session.add(entry)
            else:
                entry.occurrence_count += 1
                from datetime import datetime, timezone
                entry.last_seen_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception as e:
            log.debug(f"PII Guard: DB persist error: {e}")

        placeholder = f"[PII:{entity_type}:{token}]"
        result = result[:start] + placeholder + result[end:]

    return result


def _log_pii_detection(
    session_id: str | None,
    message_id: str | None,
    direction: str,
    entity_types: list[str],
    count: int,
    sources: list[str],
):
    """Log a PII detection event to the pii_logs table."""
    try:
        from app import db
        from app.models.logs import PIILog
        entry = PIILog(
            session_id=session_id,
            message_id=message_id,
            direction=direction,
            entities_found=count,
            entity_types=",".join(entity_types),
            detection_sources=",".join(sources),
            hashes_created=count,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        log.debug(f"PII Guard: PIILog write error: {e}")
