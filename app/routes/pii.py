"""
Orion's Belt — PII Guard: hash listing, single-token reveal, and the
false-positive exceptions allowlist.

SECURITY: nothing in this module is ever exposed as an MCP tool. An
exception can only be created by referencing an EXISTING PIIHashEntry
hash_token (a real prior detection) — never from arbitrary freeform text —
so an LLM/agent can never poison the allowlist even indirectly; only a
human acting through this API (protected by the app's normal auth check)
can.
"""
import logging

from flask import Blueprint, jsonify, request

from app import db
from app.models.pii import PIIException, PIIHashEntry

bp = Blueprint("pii", __name__)
log = logging.getLogger("orions-belt")


# ── Hash listing (for the exceptions UI to pick a detection from) ─────────────

@bp.route("/api/pii/hashes", methods=["GET"])
def list_pii_hashes():
    """Recent detections, WITHOUT their decrypted values — use /reveal/<token>
    to see one on demand. Keeps this listing endpoint from being a one-shot
    dump of every PII value the guard has ever seen."""
    limit = min(int(request.args.get("limit", 50)), 200)
    rows = (
        PIIHashEntry.query.order_by(PIIHashEntry.last_seen_at.desc()).limit(limit).all()
    )
    return jsonify([
        {
            "hash_token": r.hash_token,
            "entity_type": r.entity_type,
            "detection_source": r.detection_source,
            "occurrence_count": r.occurrence_count,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in rows
    ])


# ── Single-token reveal ─────────────────────────────────────────────────────

@bp.route("/api/pii/reveal/<token>", methods=["GET"])
def reveal_pii_token(token):
    """Decrypt and return ONE detection's original value.

    Distinct from PIIGuard.restore(), which replaces every [PII:TYPE:hash]
    token found in a larger string — this reveals exactly one, on request,
    for a human reviewing a specific detection before deciding whether to
    except it.
    """
    entry = PIIHashEntry.query.filter_by(hash_token=token).first()
    if not entry:
        return jsonify({"error": "Unknown token"}), 404
    from app.services.crypto import decrypt_data
    value = decrypt_data(entry.original_value)
    if value is None:
        value = entry.original_value  # legacy plaintext rows written before encryption
    return jsonify({
        "hash_token": entry.hash_token,
        "entity_type": entry.entity_type,
        "value": value,
    })


# ── Exceptions allowlist ─────────────────────────────────────────────────────

@bp.route("/api/pii/exceptions", methods=["GET"])
def list_pii_exceptions():
    rows = PIIException.query.order_by(PIIException.created_at.desc()).all()
    return jsonify([e.to_dict() for e in rows])


@bp.route("/api/pii/exceptions", methods=["POST"])
def create_pii_exception():
    body = request.get_json() or {}
    hash_token = (body.get("hash_token") or "").strip()
    match_mode = (body.get("match_mode") or "exact").strip()

    if not hash_token:
        return jsonify({"error": "hash_token is required"}), 400
    if match_mode not in ("exact", "normalized", "regex"):
        return jsonify({"error": "match_mode must be 'exact', 'normalized', or 'regex'"}), 400

    # The anti-poisoning constraint: the value comes ONLY from an existing
    # detection row, never from the request body directly.
    entry = PIIHashEntry.query.filter_by(hash_token=hash_token).first()
    if not entry:
        return jsonify({"error": f"Unknown hash_token '{hash_token}' — it must reference an existing detection"}), 404

    from app.services.crypto import decrypt_data, encrypt_data
    original_value = decrypt_data(entry.original_value) or entry.original_value
    if not original_value:
        return jsonify({"error": "Could not decrypt the referenced detection"}), 500

    if match_mode == "regex":
        # The detected literal text becomes the pattern as-is (never
        # hand-authored — see the hash_token lookup above), so it can easily
        # contain regex metacharacters (phone numbers with unbalanced
        # parens, etc.) that fail to compile. Reject that at creation time
        # rather than have it silently never apply at scan time, which looks
        # to the user like "I added an exception and it did nothing."
        import regex as regex_mod
        try:
            regex_mod.compile(original_value)
        except regex_mod.error as e:
            return jsonify({"error": f"This detected text isn't a valid regex pattern: {e}"}), 400

    exc = PIIException(
        entity_type=entry.entity_type,
        match_mode=match_mode,
        value=encrypt_data(original_value),
        source_hash_token=hash_token,
    )
    db.session.add(exc)
    db.session.commit()
    log.info("PII exception created: entity_type=%s match_mode=%s source_hash_token=%s",
              entry.entity_type, match_mode, hash_token)
    return jsonify(exc.to_dict()), 201


@bp.route("/api/pii/exceptions/<exception_id>", methods=["DELETE"])
def delete_pii_exception(exception_id):
    exc = PIIException.query.get(exception_id)
    if not exc:
        return jsonify({"error": "Exception not found"}), 404
    db.session.delete(exc)
    db.session.commit()
    return "", 204
