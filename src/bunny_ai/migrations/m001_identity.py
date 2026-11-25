"""Migration 001: schema metadata and singleton Bunny identity."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create schema_migrations and identity tables."""
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            identity_id TEXT NOT NULL UNIQUE,
            seed_version TEXT NOT NULL,
            style_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            awakened_at TEXT NOT NULL,
            onboarding_state TEXT NOT NULL
                CHECK (onboarding_state IN ('greeted', 'answered', 'skipped')),
            updated_at TEXT NOT NULL
        )
        """
    )
