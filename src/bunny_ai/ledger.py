"""Persistent integer micro-USD spending ledger and anti-rollback enforcement."""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from bunny_ai._watermark import (
    ATTEMPT_COLUMNS,
    DISPATCH_LOCK_FILENAME,
    LEDGER_LOCK_FILENAME,
    AttemptRecord,
    LedgerWatermark,
    attempt_from_row,
    merge_watermarks,
    read_db_ledger_state,
    read_watermark,
    reconcile_db_with_watermark,
    write_watermark_atomic,
)
from bunny_ai.budget import (
    PriceProfile,
    calculate_settlement_micro_usd,
    format_micro_usd,
    price_profile_from_json,
    price_profile_to_json,
)
from bunny_ai.config import BudgetSettings, ensure_data_dir, restrict_path_permissions
from bunny_ai.errors import (
    BudgetError,
    DatabaseLockedError,
    DispatchBlockedError,
    GatewayError,
    PricingError,
)
from bunny_ai.providers.types import ProviderReceipt
from bunny_ai.storage import (
    LocalStorage,
    _try_lock_fd,
    _unlock_fd,
    acquire_exclusive_file_lock,
)

MIN_ATTEMPT_REF_CHARS = 4


def _iso_utc(now_utc: datetime | None) -> str:
    dt = datetime.now(UTC) if now_utc is None else now_utc
    utc_dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _FactLike(Protocol):
    @property
    def fact_id(self) -> str: ...
    @property
    def category(self) -> str: ...
    @property
    def value(self) -> str: ...


def compute_facts_digest(
    facts: Sequence[tuple[str, str, str] | _FactLike],
) -> str:
    """Compute a deterministic SHA-256 digest of durable fact triples."""
    normalized: list[tuple[str, str, str]] = []
    for item in facts:
        if isinstance(item, tuple):
            normalized.append((str(item[0]), str(item[1]), str(item[2])))
        else:
            normalized.append((str(item.fact_id), str(item.category), str(item.value)))
    normalized.sort()
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_consent_digest_from_parts(
    memory_enabled: bool,
    context_floor: str | None,
    facts_digest: str,
) -> str:
    """Combine memory switch, floor, and ``facts_digest`` into a consent digest."""
    payload = {
        "memory_enabled": bool(memory_enabled),
        "context_floor": context_floor,
        "facts_digest": facts_digest,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_consent_digest(
    memory_enabled: bool,
    context_floor: str | None,
    facts: Sequence[tuple[str, str, str] | _FactLike],
) -> str:
    """Compute a deterministic SHA-256 digest of memory consent and durable facts."""
    return compute_consent_digest_from_parts(
        memory_enabled, context_floor, compute_facts_digest(facts)
    )


def compute_consent_digest_from_conn(
    conn: sqlite3.Connection, identity_id: str
) -> tuple[bool, str | None, str, str]:
    """Read live memory consent state inside ``conn`` and return digests.

    Returns ``(memory_enabled, context_floor, facts_digest, consent_digest)``.
    """
    settings_row = conn.execute(
        "SELECT enabled, context_floor FROM memory_settings WHERE singleton = 1"
    ).fetchone()
    if settings_row is None:
        raise GatewayError("Memory settings row is missing.")
    memory_enabled = bool(settings_row[0])
    context_floor = None if settings_row[1] is None else str(settings_row[1])
    fact_rows = conn.execute(
        """
        SELECT fact_id, category, value
        FROM memory_facts
        WHERE identity_id = ?
        ORDER BY fact_id ASC
        """,
        (identity_id,),
    ).fetchall()
    facts = [(str(r[0]), str(r[1]), str(r[2])) for r in fact_rows]
    facts_digest = compute_facts_digest(facts)
    consent_digest = compute_consent_digest_from_parts(
        memory_enabled, context_floor, facts_digest
    )
    return memory_enabled, context_floor, facts_digest, consent_digest


@dataclass(frozen=True)
class LedgerSummary:
    """Current budget policy, spend totals, and block status."""

    identity_id: str
    per_request_micro_usd: int | None
    cumulative_micro_usd: int | None
    settled_micro_usd: int
    held_micro_usd: int
    available_micro_usd: int | None
    dispatch_blocked: bool
    block_reason: str | None
    block_attempt_id: str | None
    attempt_count: int
    settled_count: int
    refused_count: int
    released_count: int
    uncertain_hold_count: int
    overrun_blocked_count: int


class BudgetLedger:
    """SQLite-backed spending ledger with companion watermark anti-rollback."""

    def __init__(self, storage: LocalStorage, identity_id: str) -> None:
        self._storage = storage
        self._identity_id = identity_id

    @property
    def identity_id(self) -> str:
        return self._identity_id

    @contextmanager
    def _ledger_lock(self) -> Iterator[None]:
        lock_path = self._storage.data_dir / LEDGER_LOCK_FILENAME
        with acquire_exclusive_file_lock(lock_path, self._storage.busy_timeout_ms):
            yield

    @contextmanager
    def dispatch_lock(self) -> Iterator[None]:
        """Hold a shared OS lock while an upstream transport call is in flight."""
        ensure_data_dir(self._storage.data_dir)
        lock_path = self._storage.data_dir / DISPATCH_LOCK_FILENAME
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            restrict_path_permissions(lock_path, is_dir=False)
            deadline = time.monotonic() + (self._storage.busy_timeout_ms / 1000.0)
            while True:
                if _try_lock_fd(fd, exclusive=False):
                    locked = True
                    break
                if time.monotonic() >= deadline:
                    raise DatabaseLockedError(
                        f"Timed out waiting for dispatch lock '{lock_path}'."
                    )
                time.sleep(0.01 + random.uniform(0.0, 0.02))
            yield
        finally:
            if locked:
                _unlock_fd(fd)
            else:
                os.close(fd)

    def _write_watermark_from_conn(self, conn: sqlite3.Connection) -> None:
        state = read_db_ledger_state(conn, self._identity_id)
        existing = read_watermark(self._storage.data_dir)
        if existing is not None and existing.identity_id == self._identity_id:
            state = merge_watermarks(state, existing)
        write_watermark_atomic(self._storage.data_dir, state)

    def _fetch_attempt(
        self, conn: sqlite3.Connection, attempt_id: str
    ) -> AttemptRecord | None:
        row = conn.execute(
            f"SELECT {ATTEMPT_COLUMNS} FROM gateway_attempts WHERE attempt_id = ? AND identity_id = ?",
            (attempt_id, self._identity_id),
        ).fetchone()
        return None if row is None else attempt_from_row(tuple(row))

    def _require_attempt(
        self, conn: sqlite3.Connection, attempt_id: str
    ) -> AttemptRecord:
        attempt = self._fetch_attempt(conn, attempt_id)
        if attempt is None:
            raise GatewayError(f"Unknown gateway attempt '{attempt_id}'.")
        return attempt

    def check_and_reconcile_watermark(self, now_utc: datetime | None = None) -> bool:
        """Verify DB against companion watermark; block and carry forward if stale."""
        now_iso = _iso_utc(now_utc)
        with self._ledger_lock():
            wm = read_watermark(self._storage.data_dir)
            with self._storage.transaction("reconcile ledger watermark") as conn:
                if wm is None:
                    db_state = read_db_ledger_state(conn, self._identity_id)
                    write_watermark_atomic(self._storage.data_dir, db_state)
                    return False
                stale, _, merged = reconcile_db_with_watermark(
                    conn,
                    self._identity_id,
                    wm,
                    block_reason="stale_backup_detected_reconcile_required",
                    now_iso=now_iso,
                )
                write_watermark_atomic(self._storage.data_dir, merged)
                return stale

    def recover_stale_intents(self, now_utc: datetime | None = None) -> int:
        """Promote orphaned ``reserved`` intents to ``uncertain_hold`` if no dispatcher runs."""
        ensure_data_dir(self._storage.data_dir)
        lock_path = self._storage.data_dir / DISPATCH_LOCK_FILENAME
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            restrict_path_permissions(lock_path, is_dir=False)
            if not _try_lock_fd(fd, exclusive=True):
                return 0
            locked = True
            now_iso = _iso_utc(now_utc)
            with self._ledger_lock():
                with self._storage.transaction("recover stale gateway intents") as conn:
                    cursor = conn.execute(
                        """
                        UPDATE gateway_attempts
                        SET state = 'uncertain_hold',
                            reason = 'recovered_after_crash',
                            updated_at = ?
                        WHERE identity_id = ? AND state = 'reserved'
                        """,
                        (now_iso, self._identity_id),
                    )
                    recovered = max(int(cursor.rowcount), 0)
                if recovered > 0:
                    with self._storage.read_transaction(
                        "sync watermark after crash recovery"
                    ) as conn:
                        self._write_watermark_from_conn(conn)
            return recovered
        finally:
            if locked:
                _unlock_fd(fd)
            else:
                os.close(fd)

    def sync_policy_from_config(
        self, budget_settings: BudgetSettings, now_utc: datetime | None = None
    ) -> None:
        """Apply explicit config budget caps to ``budget_policy``."""
        if (
            budget_settings.per_request_micro_usd is None
            and budget_settings.cumulative_micro_usd is None
        ):
            return
        self.set_allowances(
            budget_settings.per_request_micro_usd,
            budget_settings.cumulative_micro_usd,
            now_utc,
        )

    def set_allowances(
        self,
        per_request_micro_usd: int | None = None,
        cumulative_micro_usd: int | None = None,
        now_utc: datetime | None = None,
    ) -> LedgerSummary:
        """Update per-request and/or cumulative spending caps."""
        if per_request_micro_usd is None and cumulative_micro_usd is None:
            raise BudgetError("Specify at least one allowance to update.")
        for label, val in (
            ("per_request_micro_usd", per_request_micro_usd),
            ("cumulative_micro_usd", cumulative_micro_usd),
        ):
            if val is not None and (
                isinstance(val, bool) or not isinstance(val, int) or val < 0
            ):
                raise BudgetError(f"{label} must be a non-negative integer.")
        now_iso = _iso_utc(now_utc)
        with self._ledger_lock():
            with self._storage.transaction("update budget policy") as conn:
                if per_request_micro_usd is not None:
                    conn.execute(
                        "UPDATE budget_policy SET per_request_micro_usd = ?, updated_at = ? WHERE singleton = 1",
                        (per_request_micro_usd, now_iso),
                    )
                if cumulative_micro_usd is not None:
                    conn.execute(
                        "UPDATE budget_policy SET cumulative_micro_usd = ?, updated_at = ? WHERE singleton = 1",
                        (cumulative_micro_usd, now_iso),
                    )
            with self._storage.read_transaction(
                "sync watermark after policy update"
            ) as conn:
                self._write_watermark_from_conn(conn)
        return self.summary()

    def record_refusal(
        self,
        *,
        attempt_id: str,
        idempotency_key: str,
        profile_id: str,
        provider_id: str,
        model_id: str,
        tier: str,
        price_version: str,
        frozen_price_json: str,
        request_digest: str,
        consent_digest: str,
        refusal_code: str,
        reason: str,
        now_utc: datetime | None = None,
    ) -> AttemptRecord:
        """Persist an audit row for a request refused before transport dispatch."""
        now_iso = _iso_utc(now_utc)
        with self._storage.transaction("record gateway refusal") as conn:
            existing_attempt = self._fetch_attempt(conn, attempt_id)
            if existing_attempt is not None:
                if existing_attempt.state == "refused":
                    return existing_attempt
                effective_attempt_id = f"{attempt_id}:refused:{uuid.uuid4().hex[:8]}"
            else:
                effective_attempt_id = attempt_id

            effective_key = idempotency_key
            exists = conn.execute(
                "SELECT 1 FROM gateway_attempts WHERE idempotency_key = ?",
                (effective_key,),
            ).fetchone()
            if exists is not None:
                effective_key = f"{idempotency_key}:refused:{effective_attempt_id}"
            conn.execute(
                """
                INSERT INTO gateway_attempts (
                    attempt_id, idempotency_key, identity_id, profile_id,
                    provider_id, model_id, tier, price_version,
                    frozen_price_json, request_digest, consent_digest,
                    state, reserved_micro_usd, held_micro_usd,
                    settled_micro_usd, overrun_micro_usd, refusal_code,
                    reason, created_at, updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'refused', 0, 0, 0, 0, ?, ?, ?, ?
                )
                """,
                (
                    effective_attempt_id,
                    effective_key,
                    self._identity_id,
                    profile_id,
                    provider_id,
                    model_id,
                    tier,
                    price_version,
                    frozen_price_json,
                    request_digest,
                    consent_digest,
                    refusal_code,
                    reason,
                    now_iso,
                    now_iso,
                ),
            )
            return self._require_attempt(conn, effective_attempt_id)

    def reserve_attempt(
        self,
        *,
        attempt_id: str,
        idempotency_key: str,
        profile: PriceProfile,
        request_digest: str,
        expected_consent_digest: str,
        reserved_micro_usd: int,
        now_utc: datetime | None = None,
    ) -> AttemptRecord:
        """Atomically verify watermark, policy, consent, idempotency, and reserve funds."""
        if (
            isinstance(reserved_micro_usd, bool)
            or not isinstance(reserved_micro_usd, int)
            or reserved_micro_usd <= 0
        ):
            raise PricingError(
                "Reserved amount must be a positive integer.",
                refusal_code="pricing_incomplete",
            )
        now_iso = _iso_utc(now_utc)
        frozen_json = price_profile_to_json(profile)

        with self._ledger_lock():
            wm = read_watermark(self._storage.data_dir)
            stale_detected = False
            with self._storage.transaction("reserve gateway attempt") as conn:
                if wm is not None:
                    stale_detected, _, merged = reconcile_db_with_watermark(
                        conn,
                        self._identity_id,
                        wm,
                        block_reason="stale_backup_detected_reconcile_required",
                        now_iso=now_iso,
                    )
                    if stale_detected:
                        write_watermark_atomic(self._storage.data_dir, merged)
                if not stale_detected:
                    policy = conn.execute(
                        """
                        SELECT per_request_micro_usd, cumulative_micro_usd,
                               dispatch_blocked, block_reason
                        FROM budget_policy WHERE singleton = 1
                        """
                    ).fetchone()
                    if policy is None:
                        raise GatewayError("budget_policy singleton row is missing.")
                    per_request_cap = None if policy[0] is None else int(policy[0])
                    cumulative_cap = None if policy[1] is None else int(policy[1])
                    dispatch_blocked = bool(policy[2])
                    block_reason = None if policy[3] is None else str(policy[3])

                    if dispatch_blocked:
                        raise DispatchBlockedError(
                            f"Dispatch blocked: {block_reason or 'review required'}.",
                            refusal_code=block_reason or "dispatch_blocked",
                        )

                    _, _, _, live_consent_digest = compute_consent_digest_from_conn(
                        conn, self._identity_id
                    )
                    if live_consent_digest != expected_consent_digest:
                        raise GatewayError(
                            "Memory consent or facts changed before authorization.",
                            refusal_code="consent_changed",
                        )

                    dup_row = conn.execute(
                        "SELECT attempt_id FROM gateway_attempts WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if dup_row is not None or (
                        wm is not None
                        and any(
                            a.idempotency_key == idempotency_key for a in wm.attempts
                        )
                    ):
                        raise GatewayError(
                            f"Idempotency key '{idempotency_key}' has already been used.",
                            refusal_code="duplicate_idempotency_key",
                        )

                    if per_request_cap is None or cumulative_cap is None:
                        raise BudgetError(
                            "Explicit per-request and cumulative allowances are required before dispatch.",
                            refusal_code="allowance_missing",
                        )

                    if reserved_micro_usd > per_request_cap:
                        raise BudgetError(
                            f"Estimated reservation {format_micro_usd(reserved_micro_usd)} "
                            f"exceeds per-request limit {format_micro_usd(per_request_cap)}.",
                            refusal_code="per_request_budget_exceeded",
                        )

                    totals = conn.execute(
                        """
                        SELECT
                            COALESCE(SUM(settled_micro_usd), 0),
                            COALESCE(SUM(held_micro_usd), 0)
                        FROM gateway_attempts
                        WHERE identity_id = ?
                        """,
                        (self._identity_id,),
                    ).fetchone()
                    total_settled = int(totals[0]) if totals is not None else 0
                    total_held = int(totals[1]) if totals is not None else 0
                    if (
                        total_settled + total_held + reserved_micro_usd
                    ) > cumulative_cap:
                        available = max(0, cumulative_cap - total_settled - total_held)
                        raise BudgetError(
                            f"Estimated reservation {format_micro_usd(reserved_micro_usd)} "
                            f"exceeds available cumulative budget {format_micro_usd(available)}.",
                            refusal_code="cumulative_budget_exceeded",
                        )

                    conn.execute(
                        """
                        INSERT INTO gateway_attempts (
                            attempt_id, idempotency_key, identity_id, profile_id,
                            provider_id, model_id, tier, price_version,
                            frozen_price_json, request_digest, consent_digest,
                            state, reserved_micro_usd, held_micro_usd,
                            settled_micro_usd, overrun_micro_usd,
                            created_at, updated_at
                        )
                        VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'reserved', ?, ?, 0, 0, ?, ?
                        )
                        """,
                        (
                            attempt_id,
                            idempotency_key,
                            self._identity_id,
                            profile.profile_id,
                            profile.provider_id,
                            profile.model_id,
                            profile.tier,
                            profile.price_version,
                            frozen_json,
                            request_digest,
                            expected_consent_digest,
                            reserved_micro_usd,
                            reserved_micro_usd,
                            now_iso,
                            now_iso,
                        ),
                    )
                    attempt = self._require_attempt(conn, attempt_id)
            if stale_detected:
                raise DispatchBlockedError(
                    "Stale backup or ledger rollback detected; dispatch is blocked until reviewed.",
                    refusal_code="stale_backup_detected_reconcile_required",
                )
            with self._storage.read_transaction("sync watermark after reserve") as conn:
                self._write_watermark_from_conn(conn)
            return attempt

    def release_not_dispatched(
        self, attempt_id: str, reason: str, now_utc: datetime | None = None
    ) -> AttemptRecord:
        """Release a reserved hold when transport confirmed the request was not sent."""
        now_iso = _iso_utc(now_utc)
        with self._ledger_lock():
            with self._storage.transaction("release not-dispatched attempt") as conn:
                attempt = self._require_attempt(conn, attempt_id)
                if attempt.state != "reserved":
                    raise GatewayError(
                        f"Cannot release attempt '{attempt_id}' in state '{attempt.state}'."
                    )
                conn.execute(
                    """
                    UPDATE gateway_attempts
                    SET state = 'released',
                        held_micro_usd = 0,
                        settled_micro_usd = 0,
                        reason = ?,
                        updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (reason, now_iso, attempt_id),
                )
                updated = self._require_attempt(conn, attempt_id)
            with self._storage.read_transaction("sync watermark after release") as conn:
                self._write_watermark_from_conn(conn)
            return updated

    def mark_uncertain_hold(
        self, attempt_id: str, reason: str, now_utc: datetime | None = None
    ) -> AttemptRecord:
        """Transition a ``reserved`` attempt to ``uncertain_hold``, keeping full hold."""
        now_iso = _iso_utc(now_utc)
        with self._ledger_lock():
            with self._storage.transaction("mark uncertain hold") as conn:
                attempt = self._require_attempt(conn, attempt_id)
                if attempt.state == "uncertain_hold":
                    return attempt
                if attempt.state != "reserved":
                    raise GatewayError(
                        f"Cannot mark attempt '{attempt_id}' uncertain from state '{attempt.state}'."
                    )
                conn.execute(
                    """
                    UPDATE gateway_attempts
                    SET state = 'uncertain_hold',
                        reason = ?,
                        updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (reason, now_iso, attempt_id),
                )
                updated = self._require_attempt(conn, attempt_id)
            with self._storage.read_transaction(
                "sync watermark after uncertain hold"
            ) as conn:
                self._write_watermark_from_conn(conn)
            return updated

    def settle_receipt(
        self,
        attempt_id: str,
        receipt: ProviderReceipt,
        now_utc: datetime | None = None,
    ) -> AttemptRecord:
        """Idempotently settle an attempt from a provider receipt using frozen pricing."""
        now_iso = _iso_utc(now_utc)
        with self._ledger_lock():
            with self._storage.transaction("settle gateway receipt") as conn:
                attempt = self._require_attempt(conn, attempt_id)
                existing_rcpt = conn.execute(
                    "SELECT attempt_id FROM gateway_receipts WHERE receipt_id = ?",
                    (receipt.receipt_id,),
                ).fetchone()
                if existing_rcpt is not None:
                    if str(existing_rcpt[0]) != attempt_id:
                        raise GatewayError(
                            f"Receipt '{receipt.receipt_id}' already belongs to a different attempt."
                        )
                    if attempt.state in ("settled", "overrun_blocked"):
                        return attempt

                if receipt.attempt_id != attempt_id:
                    raise GatewayError(
                        f"Receipt attempt_id mismatch: expected {attempt_id!r}, "
                        f"got {receipt.attempt_id!r}."
                    )

                if attempt.state in ("settled", "overrun_blocked"):
                    if attempt.receipt_id == receipt.receipt_id:
                        return attempt
                    raise GatewayError(
                        f"Attempt '{attempt_id}' is already settled with receipt '{attempt.receipt_id}'."
                    )

                if attempt.state not in ("reserved", "uncertain_hold"):
                    raise GatewayError(
                        f"Cannot settle attempt '{attempt_id}' in state '{attempt.state}'."
                    )

                if (
                    receipt.provider_error is not None
                    or not receipt.complete
                    or receipt.usage is None
                ):
                    conn.execute(
                        """
                        UPDATE gateway_attempts
                        SET state = 'uncertain_hold',
                            reason = ?,
                            updated_at = ?
                        WHERE attempt_id = ?
                        """,
                        (
                            receipt.provider_error
                            or f"incomplete_receipt:{receipt.finish_reason}",
                            now_iso,
                            attempt_id,
                        ),
                    )
                    updated = self._require_attempt(conn, attempt_id)
                else:
                    frozen_profile = price_profile_from_json(attempt.frozen_price_json)
                    raw_usage_json = json.dumps(
                        asdict(receipt.usage), sort_keys=True, ensure_ascii=False
                    )
                    try:
                        calc = calculate_settlement_micro_usd(
                            receipt.usage, frozen_profile
                        )
                    except PricingError as exc:
                        conn.execute(
                            """
                            UPDATE gateway_attempts
                            SET state = 'overrun_blocked',
                                held_micro_usd = reserved_micro_usd,
                                settled_micro_usd = 0,
                                overrun_micro_usd = 0,
                                receipt_id = ?,
                                reason = ?,
                                uncached_input_tokens = ?,
                                cache_read_tokens = ?,
                                cache_write_tokens = ?,
                                output_tokens = ?,
                                reasoning_tokens = ?,
                                updated_at = ?
                            WHERE attempt_id = ?
                            """,
                            (
                                receipt.receipt_id,
                                str(exc),
                                receipt.usage.uncached_input_tokens,
                                receipt.usage.cache_read_tokens,
                                receipt.usage.cache_write_tokens,
                                receipt.usage.output_tokens,
                                receipt.usage.reasoning_tokens,
                                now_iso,
                                attempt_id,
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO gateway_receipts (
                                receipt_id, attempt_id, raw_usage_json,
                                settled_micro_usd, created_at
                            )
                            VALUES (?, ?, ?, 0, ?)
                            """,
                            (receipt.receipt_id, attempt_id, raw_usage_json, now_iso),
                        )
                        conn.execute(
                            """
                            UPDATE budget_policy
                            SET dispatch_blocked = 1,
                                block_reason = 'settlement_model_or_usage_mismatch',
                                block_attempt_id = ?,
                                updated_at = ?
                            WHERE singleton = 1
                            """,
                            (attempt_id, now_iso),
                        )
                        updated = self._require_attempt(conn, attempt_id)
                    else:
                        actual = calc.settled_micro_usd
                        if actual > attempt.reserved_micro_usd:
                            overrun = actual - attempt.reserved_micro_usd
                            conn.execute(
                                """
                                UPDATE gateway_attempts
                                SET state = 'overrun_blocked',
                                    held_micro_usd = 0,
                                    settled_micro_usd = ?,
                                    overrun_micro_usd = ?,
                                    receipt_id = ?,
                                    reason = ?,
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
                                    actual,
                                    overrun,
                                    receipt.receipt_id,
                                    (
                                        f"Settled cost {format_micro_usd(actual)} exceeded "
                                        f"reservation {format_micro_usd(attempt.reserved_micro_usd)}"
                                    ),
                                    calc.uncached_input_tokens,
                                    calc.cache_read_tokens,
                                    calc.cache_write_tokens,
                                    calc.output_tokens,
                                    calc.reasoning_tokens,
                                    now_iso,
                                    now_iso,
                                    attempt_id,
                                ),
                            )
                            conn.execute(
                                """
                                UPDATE budget_policy
                                SET dispatch_blocked = 1,
                                    block_reason = 'budget_overrun',
                                    block_attempt_id = ?,
                                    updated_at = ?
                                WHERE singleton = 1
                                """,
                                (attempt_id, now_iso),
                            )
                        else:
                            conn.execute(
                                """
                                UPDATE gateway_attempts
                                SET state = 'settled',
                                    held_micro_usd = 0,
                                    settled_micro_usd = ?,
                                    overrun_micro_usd = 0,
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
                                    actual,
                                    receipt.receipt_id,
                                    calc.uncached_input_tokens,
                                    calc.cache_read_tokens,
                                    calc.cache_write_tokens,
                                    calc.output_tokens,
                                    calc.reasoning_tokens,
                                    now_iso,
                                    now_iso,
                                    attempt_id,
                                ),
                            )
                        conn.execute(
                            """
                            INSERT INTO gateway_receipts (
                                receipt_id, attempt_id, raw_usage_json,
                                settled_micro_usd, created_at
                            )
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                receipt.receipt_id,
                                attempt_id,
                                raw_usage_json,
                                actual,
                                now_iso,
                            ),
                        )
                        updated = self._require_attempt(conn, attempt_id)
            with self._storage.read_transaction(
                "sync watermark after settlement"
            ) as conn:
                self._write_watermark_from_conn(conn)
            return updated

    def resolve_attempt(self, ref: str) -> AttemptRecord:
        """Resolve an attempt by full ``attempt_id`` or unambiguous prefix."""
        needle = ref.strip().lower()
        if len(needle) < MIN_ATTEMPT_REF_CHARS:
            raise GatewayError(
                f"Use at least {MIN_ATTEMPT_REF_CHARS} characters of an attempt id."
            )
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._storage.read_transaction("resolve gateway attempt") as conn:
            rows = conn.execute(
                f"""
                SELECT {ATTEMPT_COLUMNS}
                FROM gateway_attempts
                WHERE identity_id = ?
                  AND lower(attempt_id) LIKE ? ESCAPE '\\'
                ORDER BY attempt_seq DESC
                LIMIT 2
                """,
                (self._identity_id, f"{escaped}%"),
            ).fetchall()
        matches = [attempt_from_row(tuple(row)) for row in rows]
        if not matches:
            raise GatewayError(f"No gateway attempt matches '{ref.strip()}'.")
        if len(matches) > 1:
            raise GatewayError(
                f"'{ref.strip()}' matches more than one attempt; use more characters."
            )
        return matches[0]

    def reconcile_uncertain_hold(
        self,
        attempt_ref: str,
        *,
        action: Literal["release", "settle"],
        settled_micro_usd: int | None = None,
        reason: str = "operator_reconciled",
        now_utc: datetime | None = None,
    ) -> AttemptRecord:
        """Resolve an ``uncertain_hold`` (or held ``overrun_blocked``) attempt."""
        target = self.resolve_attempt(attempt_ref)
        now_iso = _iso_utc(now_utc)
        if action not in ("release", "settle"):
            raise GatewayError(f"Unsupported reconciliation action: {action!r}")

        with self._ledger_lock():
            with self._storage.transaction("reconcile uncertain hold") as conn:
                attempt = self._require_attempt(conn, target.attempt_id)
                if attempt.state != "uncertain_hold" and not (
                    attempt.state == "overrun_blocked" and attempt.held_micro_usd > 0
                ):
                    raise GatewayError(
                        f"Attempt '{attempt.attempt_id}' is in state '{attempt.state}' "
                        "and does not have an open hold to reconcile."
                    )

                if action == "release":
                    conn.execute(
                        """
                        UPDATE gateway_attempts
                        SET state = 'released',
                            held_micro_usd = 0,
                            settled_micro_usd = 0,
                            overrun_micro_usd = 0,
                            reason = ?,
                            updated_at = ?
                        WHERE attempt_id = ?
                        """,
                        (reason, now_iso, attempt.attempt_id),
                    )
                else:
                    amount = (
                        attempt.reserved_micro_usd
                        if settled_micro_usd is None
                        else settled_micro_usd
                    )
                    if (
                        isinstance(amount, bool)
                        or not isinstance(amount, int)
                        or amount < 0
                    ):
                        raise BudgetError(
                            "Reconciled settled_micro_usd must be a non-negative integer."
                        )
                    if amount > attempt.reserved_micro_usd:
                        overrun = amount - attempt.reserved_micro_usd
                        conn.execute(
                            """
                            UPDATE gateway_attempts
                            SET state = 'overrun_blocked',
                                held_micro_usd = 0,
                                settled_micro_usd = ?,
                                overrun_micro_usd = ?,
                                reason = ?,
                                updated_at = ?,
                                settled_at = ?
                            WHERE attempt_id = ?
                            """,
                            (
                                amount,
                                overrun,
                                reason,
                                now_iso,
                                now_iso,
                                attempt.attempt_id,
                            ),
                        )
                        conn.execute(
                            """
                            UPDATE budget_policy
                            SET dispatch_blocked = 1,
                                block_reason = 'budget_overrun',
                                block_attempt_id = ?,
                                updated_at = ?
                            WHERE singleton = 1
                            """,
                            (attempt.attempt_id, now_iso),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE gateway_attempts
                            SET state = 'settled',
                                held_micro_usd = 0,
                                settled_micro_usd = ?,
                                overrun_micro_usd = 0,
                                reason = ?,
                                updated_at = ?,
                                settled_at = ?
                            WHERE attempt_id = ?
                            """,
                            (amount, reason, now_iso, now_iso, attempt.attempt_id),
                        )
                updated = self._require_attempt(conn, attempt.attempt_id)
            with self._storage.read_transaction(
                "sync watermark after reconcile"
            ) as conn:
                self._write_watermark_from_conn(conn)
            return updated

    def clear_dispatch_block(
        self,
        reason: str = "operator_reviewed",
        now_utc: datetime | None = None,
    ) -> LedgerSummary:
        """Clear ``dispatch_blocked`` after operator review."""
        _ = reason
        now_iso = _iso_utc(now_utc)
        with self._ledger_lock():
            existing = read_watermark(self._storage.data_dir)
            with self._storage.transaction("clear dispatch block") as conn:
                if existing is not None:
                    reconcile_db_with_watermark(
                        conn,
                        self._identity_id,
                        existing,
                        block_reason=existing.block_reason
                        or "stale_backup_detected_reconcile_required",
                        now_iso=now_iso,
                    )
                conn.execute(
                    """
                    UPDATE budget_policy
                    SET dispatch_blocked = 0,
                        block_reason = NULL,
                        block_attempt_id = NULL,
                        updated_at = ?
                    WHERE singleton = 1
                    """,
                    (now_iso,),
                )
                state = read_db_ledger_state(conn, self._identity_id)
                if existing is not None and existing.identity_id == self._identity_id:
                    state = LedgerWatermark(
                        identity_id=state.identity_id,
                        highest_attempt_seq=max(
                            state.highest_attempt_seq, existing.highest_attempt_seq
                        ),
                        per_request_micro_usd=state.per_request_micro_usd,
                        cumulative_micro_usd=state.cumulative_micro_usd,
                        dispatch_blocked=False,
                        block_reason=None,
                        block_attempt_id=None,
                        attempts=state.attempts,
                        receipts=state.receipts,
                        updated_at=now_iso,
                    )
            write_watermark_atomic(self._storage.data_dir, state)
        return self.summary()

    def summary(self) -> LedgerSummary:
        """Return a consistent summary of budget policy, spend, holds, and counts."""
        with self._storage.read_transaction("read ledger summary") as conn:
            policy = conn.execute(
                """
                SELECT per_request_micro_usd, cumulative_micro_usd,
                       dispatch_blocked, block_reason, block_attempt_id
                FROM budget_policy WHERE singleton = 1
                """
            ).fetchone()
            if policy is None:
                raise GatewayError("budget_policy singleton row is missing.")
            per_req = None if policy[0] is None else int(policy[0])
            cum = None if policy[1] is None else int(policy[1])
            blocked = bool(policy[2])
            block_reason = None if policy[3] is None else str(policy[3])
            block_attempt_id = None if policy[4] is None else str(policy[4])

            agg = conn.execute(
                """
                SELECT
                    COALESCE(SUM(settled_micro_usd), 0),
                    COALESCE(SUM(held_micro_usd), 0),
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN state = 'settled' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN state = 'refused' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN state = 'released' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN state = 'uncertain_hold' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN state = 'overrun_blocked' THEN 1 ELSE 0 END), 0)
                FROM gateway_attempts
                WHERE identity_id = ?
                """,
                (self._identity_id,),
            ).fetchone()
            assert agg is not None
            settled = int(agg[0])
            held = int(agg[1])
            available = max(0, cum - settled - held) if cum is not None else None
            return LedgerSummary(
                identity_id=self._identity_id,
                per_request_micro_usd=per_req,
                cumulative_micro_usd=cum,
                settled_micro_usd=settled,
                held_micro_usd=held,
                available_micro_usd=available,
                dispatch_blocked=blocked,
                block_reason=block_reason,
                block_attempt_id=block_attempt_id,
                attempt_count=int(agg[2]),
                settled_count=int(agg[3]),
                refused_count=int(agg[4]),
                released_count=int(agg[5]),
                uncertain_hold_count=int(agg[6]),
                overrun_blocked_count=int(agg[7]),
            )

    def list_attempts(self, limit: int = 20) -> tuple[AttemptRecord, ...]:
        """Return most recent gateway attempts ordered newest first."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer.")
        with self._storage.read_transaction("list gateway attempts") as conn:
            rows = conn.execute(
                f"""
                SELECT {ATTEMPT_COLUMNS}
                FROM gateway_attempts
                WHERE identity_id = ?
                ORDER BY attempt_seq DESC
                LIMIT ?
                """,
                (self._identity_id, limit),
            ).fetchall()
            return tuple(attempt_from_row(tuple(row)) for row in rows)


__all__ = [
    "AttemptRecord",
    "BudgetLedger",
    "LedgerSummary",
    "compute_consent_digest",
    "compute_consent_digest_from_conn",
    "compute_consent_digest_from_parts",
    "compute_facts_digest",
]
