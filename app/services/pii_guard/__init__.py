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

_TORCH_DLL_FIX = (
    "Fix: reinstall PyTorch CPU-only build — "
    "pip install torch --index-url https://download.pytorch.org/whl/cpu"
)

_instance: Optional["PIIGuard"] = None
_lock = threading.Lock()

# Cached torch availability — checked once so stages 2 & 3 don't each
# spend ~3 seconds waiting for the DLL failure to propagate.
_torch_ok: Optional[bool] = None
_torch_lock = threading.Lock()


def _is_torch_available() -> bool:
    """Import torch once and cache the result."""
    global _torch_ok
    if _torch_ok is not None:
        return _torch_ok
    with _torch_lock:
        if _torch_ok is not None:
            return _torch_ok
        try:
            import torch  # noqa: F401
            _torch_ok = True
        except OSError as e:
            if "1114" in str(e) or "c10.dll" in str(e) or "DLL" in str(e):
                log.warning(
                    f"PII Guard: PyTorch DLL failed to load — stages 2 & 3 disabled. "
                    f"{_TORCH_DLL_FIX}"
                )
            else:
                log.warning(f"PII Guard: torch import failed ({e}) — stages 2 & 3 disabled")
            _torch_ok = False
        except Exception as e:
            log.warning(f"PII Guard: torch import failed ({e}) — stages 2 & 3 disabled")
            _torch_ok = False
    return _torch_ok


def get_pii_guard() -> "PIIGuard":
    """Return the singleton PIIGuard instance (created on first call)."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = PIIGuard()
    return _instance


# ── Pure-regex fallback patterns (no torch, no spaCy) ────────────────────────
# Used when Presidio/spaCy can't load, giving basic coverage for the most
# common PII types that appear in enterprise chat (US-focused).
_REGEX_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL_ADDRESS",    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("PHONE_NUMBER",     re.compile(r"\b(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}\b")),
    ("US_SSN",           re.compile(r"\b(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}\b")),
    ("CREDIT_CARD",      re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b")),
    ("IP_ADDRESS",       re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")),
    ("DATE_OF_BIRTH",    re.compile(r"\b(?:dob|date of birth|born)[:\s]+\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b", re.IGNORECASE)),
    ("US_PASSPORT",      re.compile(r"\b[A-Z]{1,2}\d{6,9}\b")),
    ("IBAN_CODE",        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}(?:[A-Z0-9]{0,16})?\b")),
]


class PIIGuard:
    """Three-stage PII/PHI detection and hashing pipeline."""

    def __init__(self):
        self._presidio_ready = False
        self._regex_ready = False      # fallback when presidio/spaCy unavailable
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
        """Load Presidio with explicit spaCy NLP engine to avoid triggering torch."""
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            # Explicitly configure spaCy so presidio never falls through to
            # a transformer-based engine (which would import torch and fail if
            # the torch DLL is broken on Windows).
            nlp_config = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
            try:
                provider = NlpEngineProvider(nlp_configuration=nlp_config)
                nlp_engine = provider.create_engine()
                self._presidio_analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
            except Exception:
                # en_core_web_sm not downloaded yet — try default init
                self._presidio_analyzer = AnalyzerEngine()

            self._presidio_ready = True
            log.info("PII Guard: Presidio analyzer loaded (Stage 1 active)")
        except OSError as e:
            if "1114" in str(e) or "c10.dll" in str(e) or "DLL" in str(e):
                log.warning(
                    f"PII Guard: Presidio unavailable — torch DLL failed ({e}). "
                    f"{_TORCH_DLL_FIX} — falling back to regex scanner"
                )
            else:
                log.warning(f"PII Guard: Presidio unavailable ({e}) — falling back to regex scanner")
            self._regex_ready = True
            log.info("PII Guard: Regex fallback scanner active (Stage 1 degraded — common PII patterns only)")
        except Exception as e:
            log.warning(f"PII Guard: Presidio unavailable ({e}) — falling back to regex scanner")
            self._regex_ready = True
            log.info("PII Guard: Regex fallback scanner active (Stage 1 degraded — common PII patterns only)")

    def _init_ner(self):
        if not _is_torch_available():
            log.info("PII Guard: Stage 2 (NER) skipped — torch unavailable")
            return
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
        if not _is_torch_available():
            log.info("PII Guard: Stage 3 (judge) skipped — torch unavailable")
            return
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

        # Stage 1a: Presidio (full NLP-backed detection)
        if self._presidio_ready:
            try:
                results = self._presidio_analyzer.analyze(text=text, language="en")
                for r in results:
                    spans.append((r.start, r.end, r.entity_type, text[r.start:r.end], "presidio"))
            except Exception as e:
                log.debug(f"PII Guard: Presidio scan error: {e}")

        # Stage 1b: Regex fallback (runs when Presidio/spaCy can't load, e.g. torch DLL failure)
        if self._regex_ready and not self._presidio_ready:
            for entity_type, pattern in _REGEX_PATTERNS:
                for m in pattern.finditer(text):
                    spans.append((m.start(), m.end(), entity_type, m.group(), "regex"))

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
        if self._regex_ready:
            return "degraded"  # regex-only mode: basic coverage, no NER/judge
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
