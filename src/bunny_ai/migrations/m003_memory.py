"""Migration 003: user-controlled durable memory facts and settings."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create memory settings, explicit fact records and deletion tombstones."""
    conn.execute(
        """
        CREATE TABLE memory_settings (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            context_floor TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO memory_settings (singleton, enabled, context_floor, updated_at)
        VALUES (1, 1, NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """
    )
    conn.execute(
        """
        CREATE TABLE memory_facts (
            fact_id TEXT PRIMARY KEY,
            identity_id TEXT NOT NULL
                REFERENCES identity(identity_id) ON DELETE RESTRICT,
            category TEXT NOT NULL
                CHECK (category IN ('preferred_name', 'preference')),
            value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 200),
            source TEXT NOT NULL CHECK (source IN ('explicit_command')),
            confirmation TEXT NOT NULL
                CHECK (confirmation IN ('user_confirmed_local')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            correction_count INTEGER NOT NULL DEFAULT 0
                CHECK (correction_count >= 0),
            sensitivity TEXT NOT NULL CHECK (sensitivity IN ('normal')),
            retention TEXT NOT NULL CHECK (retention IN ('until_deleted')),
            imported_at TEXT,
            imported_from TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_memory_single_preferred_name
        ON memory_facts (identity_id)
        WHERE category = 'preferred_name'
        """
    )
    conn.execute(
        """
        CREATE TABLE memory_tombstones (
            fact_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL
        )
        """
    )
