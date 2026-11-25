"""Migration 002: opt-in local sessions and conversation turns."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Create sessions and turns tables with indexes."""
    conn.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            identity_id TEXT NOT NULL
                REFERENCES identity(identity_id) ON DELETE RESTRICT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            history_enabled INTEGER NOT NULL CHECK (history_enabled IN (0, 1))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL
                REFERENCES sessions(session_id) ON DELETE CASCADE,
            turn_index INTEGER NOT NULL CHECK (turn_index >= 1),
            user_text TEXT NOT NULL,
            assistant_text TEXT NOT NULL,
            provider_mode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (session_id, turn_index)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_turns_session_order
        ON turns (session_id, turn_index)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_sessions_started_at
        ON sessions (started_at)
        """
    )
