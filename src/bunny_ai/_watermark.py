"""Monotonic companion ledger watermark and anti-rollback reconciliation."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from bunny_ai.config import ensure_data_dir, restrict_path_permissions
from bunny_ai.errors import StorageError

LEDGER_WATERMARK_FILENAME = ".bunny.ledger.json"
LEDGER_LOCK_FILENAME = ".bunny.ledger.lock"
DISPATCH_LOCK_FILENAME = ".bunny.dispatch.lock"
WATERMARK_FORMAT = "bunny-ai.ledger-watermark"
WATERMARK_VERSION = 1

AttemptState = Literal[
    "refused",
    "reserved",
    "released",
    "settled",
    "uncertain_hold",
    "overrun_blocked",
]

_STATE_RANK: dict[str, int] = {
    "refused": 0,
    "reserved": 1,
    "uncertain_hold": 2,
    "released": 3,
    "settled": 3,
    "overrun_blocked": 3,
}

ATTEMPT_COLUMNS = (
    "attempt_seq, attempt_id, idempotency_key, identity_id, profile_id, "
    "provider_id, model_id, tier, price_version, frozen_price_json, "
    "request_digest, consent_digest, state, reserved_micro_usd, "
    "held_micro_usd, settled_micro_usd, overrun_micro_usd, refusal_code, "
    "reason, receipt_id, uncached_input_tokens, cache_read_tokens, "
    "cache_write_tokens, output_tokens, reasoning_tokens, created_at, "
    "updated_at, settled_at"
)


@dataclass(frozen=True)
class AttemptRecord:
    """Persisted gateway attempt record."""

    attempt_seq: int
    attempt_id: str
    idempotency_key: str
    identity_id: str
    profile_id: str
    provider_id: str
    model_id: str
    tier: str
    price_version: str
    frozen_price_json: str
    request_digest: str
    consent_digest: str
    state: AttemptState
    reserved_micro_usd: int
    held_micro_usd: int
    settled_micro_usd: int
    overrun_micro_usd: int
    refusal_code: str | None
    reason: str | None
    receipt_id: str | None
    uncached_input_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    created_at: str
    updated_at: str
    settled_at: str | None


@dataclass(frozen=True)
class ReceiptRecord:
    """Persisted provider receipt audit record."""

    receipt_id: str
    attempt_id: str
    raw_usage_json: str | None
    settled_micro_usd: int
    created_at: str


@dataclass(frozen=True)
class LedgerWatermark:
    """Monotonic companion ledger snapshot persisted outside SQLite."""

    identity_id: str
    highest_attempt_seq: int
    per_request_micro_usd: int | None
    cumulative_micro_usd: int | None
    dispatch_blocked: bool
    block_reason: str | None
    block_attempt_id: str | None
    attempts: tuple[AttemptRecord, ...]
    receipts: tuple[ReceiptRecord, ...]
    updated_at: str


def _opt_int(val: object) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise StorageError(f"Expected integer or null in watermark, got {val!r}.")
    return val


def _req_int(val: object, *, label: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise StorageError(f"Expected integer for '{label}' in watermark, got {val!r}.")
    return val


def _opt_str(val: object) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise StorageError(f"Expected string or null in watermark, got {val!r}.")
    return val


def _req_str(val: object, *, label: str) -> str:
    if not isinstance(val, str):
        raise StorageError(f"Expected string for '{label}' in watermark, got {val!r}.")
    return val


def attempt_from_row(row: tuple[object, ...]) -> AttemptRecord:
    """Convert a SQLite row ordered by ``ATTEMPT_COLUMNS`` into ``AttemptRecord``."""
    state = str(row[12])
    if state not in _STATE_RANK:
        raise StorageError(f"Unexpected gateway_attempts.state value: {state!r}")
    return AttemptRecord(
        attempt_seq=int(str(row[0])),
        attempt_id=str(row[1]),
        idempotency_key=str(row[2]),
        identity_id=str(row[3]),
        profile_id=str(row[4]),
        provider_id=str(row[5]),
        model_id=str(row[6]),
        tier=str(row[7]),
        price_version=str(row[8]),
        frozen_price_json=str(row[9]),
        request_digest=str(row[10]),
        consent_digest=str(row[11]),
        state=cast(AttemptState, state),
        reserved_micro_usd=int(str(row[13])),
        held_micro_usd=int(str(row[14])),
        settled_micro_usd=int(str(row[15])),
        overrun_micro_usd=int(str(row[16])),
        refusal_code=None if row[17] is None else str(row[17]),
        reason=None if row[18] is None else str(row[18]),
        receipt_id=None if row[19] is None else str(row[19]),
        uncached_input_tokens=None if row[20] is None else int(str(row[20])),
        cache_read_tokens=None if row[21] is None else int(str(row[21])),
        cache_write_tokens=None if row[22] is None else int(str(row[22])),
        output_tokens=None if row[23] is None else int(str(row[23])),
        reasoning_tokens=None if row[24] is None else int(str(row[24])),
        created_at=str(row[25]),
        updated_at=str(row[26]),
        settled_at=None if row[27] is None else str(row[27]),
    )


def _attempt_from_dict(raw: Mapping[str, object]) -> AttemptRecord:
    state = _req_str(raw.get("state"), label="state")
    if state not in _STATE_RANK or state == "refused":
        raise StorageError(f"Invalid attempt state in ledger watermark: {state!r}")
    return AttemptRecord(
        attempt_seq=_req_int(raw.get("attempt_seq"), label="attempt_seq"),
        attempt_id=_req_str(raw.get("attempt_id"), label="attempt_id"),
        idempotency_key=_req_str(raw.get("idempotency_key"), label="idempotency_key"),
        identity_id=_req_str(raw.get("identity_id"), label="identity_id"),
        profile_id=_req_str(raw.get("profile_id"), label="profile_id"),
        provider_id=_req_str(raw.get("provider_id"), label="provider_id"),
        model_id=_req_str(raw.get("model_id"), label="model_id"),
        tier=_req_str(raw.get("tier"), label="tier"),
        price_version=_req_str(raw.get("price_version"), label="price_version"),
        frozen_price_json=_req_str(
            raw.get("frozen_price_json"), label="frozen_price_json"
        ),
        request_digest=_req_str(raw.get("request_digest"), label="request_digest"),
        consent_digest=_req_str(raw.get("consent_digest"), label="consent_digest"),
        state=cast(AttemptState, state),
        reserved_micro_usd=_req_int(
            raw.get("reserved_micro_usd"), label="reserved_micro_usd"
        ),
        held_micro_usd=_req_int(raw.get("held_micro_usd"), label="held_micro_usd"),
        settled_micro_usd=_req_int(
            raw.get("settled_micro_usd"), label="settled_micro_usd"
        ),
        overrun_micro_usd=_req_int(
            raw.get("overrun_micro_usd"), label="overrun_micro_usd"
        ),
        refusal_code=_opt_str(raw.get("refusal_code")),
        reason=_opt_str(raw.get("reason")),
        receipt_id=_opt_str(raw.get("receipt_id")),
        uncached_input_tokens=_opt_int(raw.get("uncached_input_tokens")),
        cache_read_tokens=_opt_int(raw.get("cache_read_tokens")),
        cache_write_tokens=_opt_int(raw.get("cache_write_tokens")),
        output_tokens=_opt_int(raw.get("output_tokens")),
        reasoning_tokens=_opt_int(raw.get("reasoning_tokens")),
        created_at=_req_str(raw.get("created_at"), label="created_at"),
        updated_at=_req_str(raw.get("updated_at"), label="updated_at"),
        settled_at=_opt_str(raw.get("settled_at")),
    )


def _receipt_from_dict(raw: Mapping[str, object]) -> ReceiptRecord:
    return ReceiptRecord(
        receipt_id=_req_str(raw.get("receipt_id"), label="receipt_id"),
        attempt_id=_req_str(raw.get("attempt_id"), label="attempt_id"),
        raw_usage_json=_opt_str(raw.get("raw_usage_json")),
        settled_micro_usd=_req_int(
            raw.get("settled_micro_usd"), label="settled_micro_usd"
        ),
        created_at=_req_str(raw.get("created_at"), label="created_at"),
    )


def watermark_path(data_dir: Path) -> Path:
    """Return the companion watermark JSON path for ``data_dir``."""
    return data_dir / LEDGER_WATERMARK_FILENAME


def read_watermark(data_dir: Path) -> LedgerWatermark | None:
    """Read ``.bunny.ledger.json`` if it exists; raise ``StorageError`` if corrupt."""
    path = watermark_path(data_dir)
    if not path.exists():
        return None
    if not path.is_file():
        raise StorageError(f"Ledger watermark path is not a regular file: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
        doc = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise StorageError(
            f"Ledger watermark '{path}' is unreadable or corrupt: {exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise StorageError(f"Ledger watermark '{path}' must be a JSON object.")
    if doc.get("format") != WATERMARK_FORMAT or doc.get("version") != WATERMARK_VERSION:
        raise StorageError(
            f"Ledger watermark '{path}' has unsupported format or version."
        )
    dispatch_blocked = doc.get("dispatch_blocked")
    if not isinstance(dispatch_blocked, bool):
        raise StorageError("Ledger watermark 'dispatch_blocked' must be a boolean.")
    raw_attempts = doc.get("attempts")
    raw_receipts = doc.get("receipts")
    if not isinstance(raw_attempts, list) or not isinstance(raw_receipts, list):
        raise StorageError("Ledger watermark attempts and receipts must be lists.")
    attempts: list[AttemptRecord] = []
    for item in raw_attempts:
        if not isinstance(item, Mapping):
            raise StorageError("Ledger watermark attempt entry must be an object.")
        attempts.append(_attempt_from_dict(item))
    receipts: list[ReceiptRecord] = []
    for item in raw_receipts:
        if not isinstance(item, Mapping):
            raise StorageError("Ledger watermark receipt entry must be an object.")
        receipts.append(_receipt_from_dict(item))

    return LedgerWatermark(
        identity_id=_req_str(doc.get("identity_id"), label="identity_id"),
        highest_attempt_seq=_req_int(
            doc.get("highest_attempt_seq"), label="highest_attempt_seq"
        ),
        per_request_micro_usd=_opt_int(doc.get("per_request_micro_usd")),
        cumulative_micro_usd=_opt_int(doc.get("cumulative_micro_usd")),
        dispatch_blocked=dispatch_blocked,
        block_reason=_opt_str(doc.get("block_reason")),
        block_attempt_id=_opt_str(doc.get("block_attempt_id")),
        attempts=tuple(attempts),
        receipts=tuple(receipts),
        updated_at=_req_str(doc.get("updated_at"), label="updated_at"),
    )


def write_watermark_atomic(data_dir: Path, watermark: LedgerWatermark) -> None:
    """Persist ``watermark`` atomically via temp file + fsync + os.replace."""
    ensure_data_dir(data_dir)
    target = watermark_path(data_dir)
    payload_dict: dict[str, Any] = {
        "format": WATERMARK_FORMAT,
        "version": WATERMARK_VERSION,
        "identity_id": watermark.identity_id,
        "highest_attempt_seq": watermark.highest_attempt_seq,
        "per_request_micro_usd": watermark.per_request_micro_usd,
        "cumulative_micro_usd": watermark.cumulative_micro_usd,
        "dispatch_blocked": watermark.dispatch_blocked,
        "block_reason": watermark.block_reason,
        "block_attempt_id": watermark.block_attempt_id,
        "attempts": [asdict(a) for a in watermark.attempts],
        "receipts": [asdict(r) for r in watermark.receipts],
        "updated_at": watermark.updated_at,
    }
    encoded = (
        json.dumps(payload_dict, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    temp = data_dir / f".{LEDGER_WATERMARK_FILENAME}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        restrict_path_permissions(temp, is_dir=False)
        os.replace(temp, target)
        restrict_path_permissions(target, is_dir=False)
    except OSError as exc:
        raise StorageError(
            f"Could not write ledger watermark '{target}': {exc}"
        ) from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_db_ledger_state(conn: sqlite3.Connection, identity_id: str) -> LedgerWatermark:
    """Read current budget policy, non-refused attempts, and receipts from SQLite."""
    policy_row = conn.execute(
        """
        SELECT per_request_micro_usd, cumulative_micro_usd,
               dispatch_blocked, block_reason, block_attempt_id, updated_at
        FROM budget_policy
        WHERE singleton = 1
        """
    ).fetchone()
    if policy_row is None:
        raise StorageError("budget_policy singleton row is missing.")

    attempt_rows = conn.execute(
        f"""
        SELECT {ATTEMPT_COLUMNS}
        FROM gateway_attempts
        WHERE identity_id = ? AND state != 'refused'
        ORDER BY attempt_seq ASC
        """,
        (identity_id,),
    ).fetchall()
    attempts = tuple(attempt_from_row(tuple(row)) for row in attempt_rows)

    receipt_rows = conn.execute(
        """
        SELECT r.receipt_id, r.attempt_id, r.raw_usage_json,
               r.settled_micro_usd, r.created_at
        FROM gateway_receipts r
        JOIN gateway_attempts a ON a.attempt_id = r.attempt_id
        WHERE a.identity_id = ?
        ORDER BY r.created_at ASC, r.receipt_id ASC
        """,
        (identity_id,),
    ).fetchall()
    receipts = tuple(
        ReceiptRecord(
            receipt_id=str(row[0]),
            attempt_id=str(row[1]),
            raw_usage_json=None if row[2] is None else str(row[2]),
            settled_micro_usd=int(row[3]),
            created_at=str(row[4]),
        )
        for row in receipt_rows
    )
    highest_seq = max((a.attempt_seq for a in attempts), default=0)
    return LedgerWatermark(
        identity_id=identity_id,
        highest_attempt_seq=highest_seq,
        per_request_micro_usd=(None if policy_row[0] is None else int(policy_row[0])),
        cumulative_micro_usd=(None if policy_row[1] is None else int(policy_row[1])),
        dispatch_blocked=bool(policy_row[2]),
        block_reason=None if policy_row[3] is None else str(policy_row[3]),
        block_attempt_id=None if policy_row[4] is None else str(policy_row[4]),
        attempts=attempts,
        receipts=receipts,
        updated_at=str(policy_row[5]),
    )


def _attempt_sort_key(a: AttemptRecord) -> tuple[int, int, int, str]:
    return (
        _STATE_RANK[a.state],
        a.settled_micro_usd,
        a.overrun_micro_usd,
        a.updated_at,
    )


def _is_attempt_ahead(candidate: AttemptRecord, current: AttemptRecord) -> bool:
    if candidate == current:
        return False
    return _attempt_sort_key(candidate) > _attempt_sort_key(current)


def merge_watermarks(
    primary: LedgerWatermark, secondary: LedgerWatermark
) -> LedgerWatermark:
    """Merge two watermarks for the same identity, preserving monotonic progress."""
    if primary.identity_id != secondary.identity_id:
        raise StorageError(
            "Ledger watermark identity_id does not match profile identity."
        )
    by_id: dict[str, AttemptRecord] = {a.attempt_id: a for a in primary.attempts}
    for item in secondary.attempts:
        existing = by_id.get(item.attempt_id)
        if existing is None or _is_attempt_ahead(item, existing):
            by_id[item.attempt_id] = item

    receipts_by_id: dict[str, ReceiptRecord] = {
        r.receipt_id: r for r in primary.receipts
    }
    for rec in secondary.receipts:
        receipts_by_id.setdefault(rec.receipt_id, rec)

    attempts = tuple(
        sorted(
            by_id.values(), key=lambda a: (a.attempt_seq, a.created_at, a.attempt_id)
        )
    )
    receipts = tuple(
        sorted(receipts_by_id.values(), key=lambda r: (r.created_at, r.receipt_id))
    )
    highest_seq = max(
        primary.highest_attempt_seq,
        secondary.highest_attempt_seq,
        max((a.attempt_seq for a in attempts), default=0),
    )
    blocked = primary.dispatch_blocked or secondary.dispatch_blocked
    block_reason = primary.block_reason or secondary.block_reason
    block_attempt_id = primary.block_attempt_id or secondary.block_attempt_id
    return LedgerWatermark(
        identity_id=primary.identity_id,
        highest_attempt_seq=highest_seq,
        per_request_micro_usd=primary.per_request_micro_usd,
        cumulative_micro_usd=primary.cumulative_micro_usd,
        dispatch_blocked=blocked,
        block_reason=block_reason,
        block_attempt_id=block_attempt_id,
        attempts=attempts,
        receipts=receipts,
        updated_at=max(primary.updated_at, secondary.updated_at),
    )


def has_stale_rollback(db_state: LedgerWatermark, watermark: LedgerWatermark) -> bool:
    """Return True if ``db_state`` is missing ledger progress recorded in ``watermark``."""
    if db_state.identity_id != watermark.identity_id:
        raise StorageError(
            "Ledger watermark identity_id does not match database identity."
        )
    if watermark.highest_attempt_seq > db_state.highest_attempt_seq:
        return True
    if watermark.dispatch_blocked and not db_state.dispatch_blocked:
        return True

    db_by_id = {a.attempt_id: a for a in db_state.attempts}
    for wm_attempt in watermark.attempts:
        db_attempt = db_by_id.get(wm_attempt.attempt_id)
        if db_attempt is None or _is_attempt_ahead(wm_attempt, db_attempt):
            return True

    db_receipt_ids = {r.receipt_id for r in db_state.receipts}
    for wm_receipt in watermark.receipts:
        if wm_receipt.receipt_id not in db_receipt_ids:
            return True

    return False


def _upsert_attempt(conn: sqlite3.Connection, attempt: AttemptRecord) -> None:
    existing = conn.execute(
        "SELECT attempt_seq FROM gateway_attempts WHERE attempt_id = ?",
        (attempt.attempt_id,),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE gateway_attempts
            SET state = ?,
                reserved_micro_usd = ?,
                held_micro_usd = ?,
                settled_micro_usd = ?,
                overrun_micro_usd = ?,
                refusal_code = ?,
                reason = ?,
                receipt_id = ?,
                uncached_input_tokens = ?,
                cache_read_tokens = ?,
                cache_write_tokens = ?,
                output_tokens = ?,
                reasoning_tokens = ?,
                updated_at = ?,
                settled_at = ?
            WHERE attempt_id = ?
            """,
            (
                attempt.state,
                attempt.reserved_micro_usd,
                attempt.held_micro_usd,
                attempt.settled_micro_usd,
                attempt.overrun_micro_usd,
                attempt.refusal_code,
                attempt.reason,
                attempt.receipt_id,
                attempt.uncached_input_tokens,
                attempt.cache_read_tokens,
                attempt.cache_write_tokens,
                attempt.output_tokens,
                attempt.reasoning_tokens,
                attempt.updated_at,
                attempt.settled_at,
                attempt.attempt_id,
            ),
        )
        return

    seq_taken = conn.execute(
        "SELECT 1 FROM gateway_attempts WHERE attempt_seq = ?",
        (attempt.attempt_seq,),
    ).fetchone()
    if seq_taken is None:
        conn.execute(
            f"""
            INSERT INTO gateway_attempts ({ATTEMPT_COLUMNS})
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                attempt.attempt_seq,
                attempt.attempt_id,
                attempt.idempotency_key,
                attempt.identity_id,
                attempt.profile_id,
                attempt.provider_id,
                attempt.model_id,
                attempt.tier,
                attempt.price_version,
                attempt.frozen_price_json,
                attempt.request_digest,
                attempt.consent_digest,
                attempt.state,
                attempt.reserved_micro_usd,
                attempt.held_micro_usd,
                attempt.settled_micro_usd,
                attempt.overrun_micro_usd,
                attempt.refusal_code,
                attempt.reason,
                attempt.receipt_id,
                attempt.uncached_input_tokens,
                attempt.cache_read_tokens,
                attempt.cache_write_tokens,
                attempt.output_tokens,
                attempt.reasoning_tokens,
                attempt.created_at,
                attempt.updated_at,
                attempt.settled_at,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO gateway_attempts (
                attempt_id, idempotency_key, identity_id, profile_id,
                provider_id, model_id, tier, price_version, frozen_price_json,
                request_digest, consent_digest, state, reserved_micro_usd,
                held_micro_usd, settled_micro_usd, overrun_micro_usd,
                refusal_code, reason, receipt_id, uncached_input_tokens,
                cache_read_tokens, cache_write_tokens, output_tokens,
                reasoning_tokens, created_at, updated_at, settled_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                attempt.attempt_id,
                attempt.idempotency_key,
                attempt.identity_id,
                attempt.profile_id,
                attempt.provider_id,
                attempt.model_id,
                attempt.tier,
                attempt.price_version,
                attempt.frozen_price_json,
                attempt.request_digest,
                attempt.consent_digest,
                attempt.state,
                attempt.reserved_micro_usd,
                attempt.held_micro_usd,
                attempt.settled_micro_usd,
                attempt.overrun_micro_usd,
                attempt.refusal_code,
                attempt.reason,
                attempt.receipt_id,
                attempt.uncached_input_tokens,
                attempt.cache_read_tokens,
                attempt.cache_write_tokens,
                attempt.output_tokens,
                attempt.reasoning_tokens,
                attempt.created_at,
                attempt.updated_at,
                attempt.settled_at,
            ),
        )


def _upsert_receipt(conn: sqlite3.Connection, receipt: ReceiptRecord) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO gateway_receipts (
            receipt_id, attempt_id, raw_usage_json, settled_micro_usd, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            receipt.receipt_id,
            receipt.attempt_id,
            receipt.raw_usage_json,
            receipt.settled_micro_usd,
            receipt.created_at,
        ),
    )


def reconcile_db_with_watermark(
    conn: sqlite3.Connection,
    identity_id: str,
    watermark: LedgerWatermark,
    *,
    block_reason: str,
    now_iso: str,
) -> tuple[bool, int, LedgerWatermark]:
    """Carry forward missing/newer ledger entries into ``conn`` inside caller's TX.

    Returns ``(stale_detected, reconciled_attempt_count, merged_watermark)``.
    """
    db_state = read_db_ledger_state(conn, identity_id)
    if not has_stale_rollback(db_state, watermark):
        merged = merge_watermarks(db_state, watermark)
        return (False, 0, merged)

    db_by_id = {a.attempt_id: a for a in db_state.attempts}
    reconciled_attempts = 0
    for wm_attempt in watermark.attempts:
        db_attempt = db_by_id.get(wm_attempt.attempt_id)
        if db_attempt is None or _is_attempt_ahead(wm_attempt, db_attempt):
            _upsert_attempt(conn, wm_attempt)
            reconciled_attempts += 1

    db_receipt_ids = {r.receipt_id for r in db_state.receipts}
    for wm_receipt in watermark.receipts:
        if wm_receipt.receipt_id not in db_receipt_ids:
            _upsert_receipt(conn, wm_receipt)

    block_attempt_id = watermark.block_attempt_id or db_state.block_attempt_id
    conn.execute(
        """
        UPDATE budget_policy
        SET dispatch_blocked = 1,
            block_reason = ?,
            block_attempt_id = ?,
            updated_at = ?
        WHERE singleton = 1
        """,
        (block_reason, block_attempt_id, now_iso),
    )

    refreshed = read_db_ledger_state(conn, identity_id)
    final_watermark = LedgerWatermark(
        identity_id=identity_id,
        highest_attempt_seq=max(
            refreshed.highest_attempt_seq, watermark.highest_attempt_seq
        ),
        per_request_micro_usd=refreshed.per_request_micro_usd,
        cumulative_micro_usd=refreshed.cumulative_micro_usd,
        dispatch_blocked=True,
        block_reason=block_reason,
        block_attempt_id=block_attempt_id,
        attempts=refreshed.attempts,
        receipts=refreshed.receipts,
        updated_at=now_iso,
    )
    return (True, reconciled_attempts, final_watermark)
