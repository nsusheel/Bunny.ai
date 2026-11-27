"""Migration 004: provider-neutral gateway budget policy, attempts, and receipts."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create budget policy, gateway attempt ledger, and receipt audit tables."""
    conn.execute(
        """
        CREATE TABLE budget_policy (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            per_request_micro_usd INTEGER
                CHECK (per_request_micro_usd IS NULL OR per_request_micro_usd >= 0),
            cumulative_micro_usd INTEGER
                CHECK (cumulative_micro_usd IS NULL OR cumulative_micro_usd >= 0),
            dispatch_blocked INTEGER NOT NULL DEFAULT 0
                CHECK (dispatch_blocked IN (0, 1)),
            block_reason TEXT,
            block_attempt_id TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO budget_policy (
            singleton, per_request_micro_usd, cumulative_micro_usd,
            dispatch_blocked, block_reason, block_attempt_id, updated_at
        )
        VALUES (
            1, 50000, 500000, 0, NULL, NULL,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE gateway_attempts (
            attempt_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL UNIQUE,
            identity_id TEXT NOT NULL
                REFERENCES identity(identity_id) ON DELETE RESTRICT,
            profile_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            price_version TEXT NOT NULL,
            frozen_price_json TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            consent_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN (
                    'refused',
                    'reserved',
                    'released',
                    'settled',
                    'uncertain_hold',
                    'overrun_blocked'
                )
            ),
            reserved_micro_usd INTEGER NOT NULL CHECK (reserved_micro_usd >= 0),
            held_micro_usd INTEGER NOT NULL CHECK (held_micro_usd >= 0),
            settled_micro_usd INTEGER NOT NULL CHECK (settled_micro_usd >= 0),
            overrun_micro_usd INTEGER NOT NULL DEFAULT 0
                CHECK (overrun_micro_usd >= 0),
            refusal_code TEXT,
            reason TEXT,
            receipt_id TEXT UNIQUE,
            uncached_input_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            output_tokens INTEGER,
            reasoning_tokens INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            settled_at TEXT,
            CHECK (
                state != 'refused' OR (
                    reserved_micro_usd = 0
                    AND held_micro_usd = 0
                    AND settled_micro_usd = 0
                    AND overrun_micro_usd = 0
                )
            ),
            CHECK (
                state != 'reserved' OR (
                    reserved_micro_usd > 0
                    AND held_micro_usd = reserved_micro_usd
                    AND settled_micro_usd = 0
                    AND overrun_micro_usd = 0
                )
            ),
            CHECK (
                state != 'released' OR (
                    held_micro_usd = 0
                    AND settled_micro_usd = 0
                    AND overrun_micro_usd = 0
                )
            ),
            CHECK (
                state != 'settled' OR (
                    reserved_micro_usd > 0
                    AND held_micro_usd = 0
                    AND settled_micro_usd <= reserved_micro_usd
                    AND overrun_micro_usd = 0
                )
            ),
            CHECK (
                state != 'uncertain_hold' OR (
                    reserved_micro_usd > 0
                    AND held_micro_usd = reserved_micro_usd
                    AND settled_micro_usd = 0
                    AND overrun_micro_usd = 0
                )
            ),
            CHECK (
                state != 'overrun_blocked' OR (
                    (
                        held_micro_usd = 0
                        AND settled_micro_usd > reserved_micro_usd
                        AND overrun_micro_usd = settled_micro_usd - reserved_micro_usd
                    )
                    OR (
                        held_micro_usd = reserved_micro_usd
                        AND settled_micro_usd = 0
                        AND overrun_micro_usd = 0
                    )
                )
            )
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_gateway_attempts_identity_state
        ON gateway_attempts (identity_id, state)
        """
    )
    conn.execute(
        """
        CREATE TABLE gateway_receipts (
            receipt_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL
                REFERENCES gateway_attempts(attempt_id) ON DELETE RESTRICT,
            raw_usage_json TEXT,
            settled_micro_usd INTEGER NOT NULL CHECK (settled_micro_usd >= 0),
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_gateway_receipts_attempt
        ON gateway_receipts (attempt_id)
        """
    )
