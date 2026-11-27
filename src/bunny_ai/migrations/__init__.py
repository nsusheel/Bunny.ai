"""Ordered SQLite schema migrations for Bunny.ai."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from bunny_ai.migrations import (
    m001_identity,
    m002_history,
    m003_memory,
    m004_gateway_ledger,
)


@dataclass(frozen=True)
class Migration:
    """Single versioned schema migration step."""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="Initial identity and migration metadata tables",
        apply=m001_identity.apply,
    ),
    Migration(
        version=2,
        description="Opt-in local sessions and conversation turns",
        apply=m002_history.apply,
    ),
    Migration(
        version=3,
        description="User-controlled memory facts, settings and tombstones",
        apply=m003_memory.apply,
    ),
    Migration(
        version=4,
        description="Provider-neutral gateway budget policy, attempts and receipts",
        apply=m004_gateway_ledger.apply,
    ),
)

CURRENT_SCHEMA_VERSION: int = MIGRATIONS[-1].version

__all__ = ["CURRENT_SCHEMA_VERSION", "MIGRATIONS", "Migration"]
