"""
Hash chain for AuditLog — tamper-evidence for the audit trail.

Every AuditLog row stores a SHA-256 hash over its own content plus the
previous row's hash (previous_hash=NULL for the very first chained row, or
for the first row written after upgrading an install that already had
unchained rows — the chain simply starts fresh from there rather than
retroactively failing on history that predates it). Changing, deleting, or
reordering a row after the fact breaks the hash of every row after it,
which verify_chain() detects without needing any external storage — no
S3/Object Lock/retention infrastructure, just a self-contained integrity
check appropriate for a local single-user tool.

This does NOT stop someone with direct sqlite3 access to orions_belt.db
from rewriting the whole chain consistently (the hash values themselves
live in the same file) — that's a different threat model (see
requirements.txt's "restrict orions_belt.db file permissions" guidance
in SECURITY_NOTES). What this protects against is inadvertent or partial
corruption/tampering that only touches some rows.
"""
from __future__ import annotations

import hashlib


def compute_row_hash(
    previous_hash: str | None,
    created_at,
    tool_name: str,
    tier: int,
    caller: str | None,
    session_id: str | None,
    run_id: str | None,
    input_summary: str | None,
    outcome: str,
    result_summary: str | None,
    error: str | None,
) -> str:
    """Deterministic SHA-256 over this row's persisted fields plus the
    previous row's hash. Field order and the "" fallback for None are
    fixed — any change here would break verification against every
    previously-written row's hash, so this function's shape itself is the
    contract, not just its output."""
    # SQLite round-trips DateTime columns as tz-naive regardless of what was
    # stored (a pre-existing, widespread gotcha in this app — see
    # test_triggers.py/test_digest.py) — created_at.isoformat() on the
    # tz-aware object built at write time and on the naive object read back
    # at verify time would otherwise differ only by the "+00:00" suffix,
    # making every row fail verification against its own freshly-written
    # hash. strftime with no %z/%Z sidesteps the whole naive-vs-aware
    # question, since every datetime in this app is implicitly UTC either way.
    parts = [
        previous_hash or "",
        created_at.strftime("%Y-%m-%dT%H:%M:%S.%f") if created_at else "",
        tool_name or "",
        str(tier),
        caller or "",
        session_id or "",
        run_id or "",
        input_summary or "",
        outcome or "",
        result_summary or "",
        error or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8", errors="replace")).hexdigest()


def get_last_row_hash() -> str | None:
    """The row_hash of the most recently created AuditLog row, or None if
    there are no rows yet, or if the most recent row predates the chain
    (a legacy row with no hash) — either way, None is the correct
    previous_hash for the next row to link from."""
    from app.models.logs import AuditLog

    last = AuditLog.query.order_by(AuditLog.created_at.desc()).first()
    return last.row_hash if last else None


def verify_chain() -> dict:
    """Walk every AuditLog row in creation order and verify the hash
    chain. Rows written before this feature existed (row_hash IS NULL)
    are skipped for verification but still reset the expected link, since
    a chained row following a legacy row correctly has previous_hash=NULL.

    Returns {"valid": bool, "total_rows": int, "chained_rows": int,
    "first_break_row_id": str | None, "reason": str | None}.
    """
    from app.models.logs import AuditLog

    rows = AuditLog.query.order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).all()
    expected_previous_hash = None
    chained_rows = 0

    for row in rows:
        if row.row_hash is None:
            # Legacy, unchained row — not a break, but the chain link
            # resets: whatever comes next (if chained) must point to None.
            expected_previous_hash = None
            continue

        if row.previous_hash != expected_previous_hash:
            return {
                "valid": False, "total_rows": len(rows), "chained_rows": chained_rows,
                "first_break_row_id": row.id,
                "reason": f"row {row.id}: previous_hash does not match the prior chained row's hash "
                          f"(row deleted, reordered, or inserted out of band)",
            }

        recomputed = compute_row_hash(
            row.previous_hash, row.created_at, row.tool_name, row.tier, row.caller,
            row.session_id, row.run_id, row.input_summary, row.outcome,
            row.result_summary, row.error,
        )
        if recomputed != row.row_hash:
            return {
                "valid": False, "total_rows": len(rows), "chained_rows": chained_rows,
                "first_break_row_id": row.id,
                "reason": f"row {row.id}: stored content does not match its own hash (modified after write)",
            }

        expected_previous_hash = row.row_hash
        chained_rows += 1

    return {"valid": True, "total_rows": len(rows), "chained_rows": chained_rows,
            "first_break_row_id": None, "reason": None}
