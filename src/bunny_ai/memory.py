"""User-controlled durable memory: explicit facts, bounded context and deletion.

Only facts the user proposed and confirmed locally are stored. Nothing here
infers facts from conversation. Fact values are untrusted data: they are never
treated as instructions and never alter consent, permissions or spending.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from bunny_ai.errors import StorageError
from bunny_ai.providers.types import ContextFact
from bunny_ai.storage import LocalStorage

Category = Literal["preferred_name", "preference"]
CATEGORIES: tuple[Category, ...] = ("preferred_name", "preference")
CATEGORY_LABELS: dict[str, str] = {
    "preferred_name": "preferred name",
    "preference": "preference",
}

MAX_FACT_CHARS = 200
MAX_FACTS = 20
MAX_CONTEXT_FACTS = 5
MAX_CONTEXT_CHARS = 500
MIN_REF_CHARS = 4

SOURCE_EXPLICIT = "explicit_command"
CONFIRMATION_LOCAL = "user_confirmed_local"
SOURCE_LABELS = {SOURCE_EXPLICIT: "you typed it with a /remember or /memory command"}
CONFIRMATION_LABELS = {
    CONFIRMATION_LOCAL: "you confirmed it locally before it was saved",
}

DELETION_LIMITS = (
    "Deleting a fact removes it from this profile's future context only. It does "
    "not remove copies in earlier exports, backups (see the profile's backups "
    "folder), saved chat history, terminal scrollback or SQLite free space."
)


class MemoryCommandError(StorageError):
    """Base error for user-facing memory command failures."""


class InvalidFactError(MemoryCommandError):
    """The proposed fact value is not acceptable."""


class MemoryDisabledError(MemoryCommandError):
    """Memory is switched off, so saving or correcting facts is refused."""


class FactNotFoundError(MemoryCommandError):
    """No fact matches the supplied reference."""


class FactLimitError(MemoryCommandError):
    """Saving would exceed the per-profile fact cap."""


@dataclass(frozen=True)
class MemoryFact:
    """One durable, user-confirmed fact with provenance."""

    fact_id: str
    identity_id: str
    category: Category
    value: str
    source: str
    confirmation: str
    created_at: str
    updated_at: str
    confirmed_at: str
    correction_count: int
    sensitivity: str
    retention: str
    imported_at: str | None
    imported_from: str | None


@dataclass(frozen=True)
class MemorySettings:
    """Memory switch and the history-context floor set by memory changes."""

    enabled: bool
    context_floor: str | None


@dataclass(frozen=True)
class ContextSelection:
    """Bounded facts for one request, plus how many were left out."""

    facts: tuple[ContextFact, ...]
    omitted: int


def utc_now_iso() -> str:
    """Return a UTC timestamp in the project's canonical format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_fact_value(raw: str) -> str:
    """Return a clean fact value or raise ``InvalidFactError``."""
    value = raw.strip()
    if not value:
        raise InvalidFactError("A memory value must not be empty.")
    if len(value) > MAX_FACT_CHARS:
        raise InvalidFactError(
            f"A memory value is limited to {MAX_FACT_CHARS} characters."
        )
    for char in value:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            raise InvalidFactError(
                "A memory value must be a single line without control characters."
            )
    return value


def fact_from_row(row: tuple[object, ...]) -> MemoryFact:
    """Build a ``MemoryFact`` from a ``SELECT *``-ordered row."""
    category = str(row[2])
    if category not in CATEGORIES:
        raise StorageError(f"Unexpected memory category in database: {category!r}")
    return MemoryFact(
        fact_id=str(row[0]),
        identity_id=str(row[1]),
        category=category,
        value=str(row[3]),
        source=str(row[4]),
        confirmation=str(row[5]),
        created_at=str(row[6]),
        updated_at=str(row[7]),
        confirmed_at=str(row[8]),
        correction_count=int(str(row[9])),
        sensitivity=str(row[10]),
        retention=str(row[11]),
        imported_at=None if row[12] is None else str(row[12]),
        imported_from=None if row[13] is None else str(row[13]),
    )


FACT_COLUMNS = (
    "fact_id, identity_id, category, value, source, confirmation, created_at, "
    "updated_at, confirmed_at, correction_count, sensitivity, retention, "
    "imported_at, imported_from"
)


class MemoryStore:
    """Durable-memory operations over one open ``LocalStorage``."""

    def __init__(self, storage: LocalStorage, identity_id: str) -> None:
        self._storage = storage
        self._identity_id = identity_id

    @property
    def identity_id(self) -> str:
        return self._identity_id

    # -- reads -------------------------------------------------------------

    def settings(self) -> MemorySettings:
        rows = self._storage.query(
            "SELECT enabled, context_floor FROM memory_settings",
            context="read memory settings",
        )
        if not rows:
            raise StorageError("Memory settings row is missing; profile not changed.")
        floor = rows[0][1]
        return MemorySettings(
            enabled=bool(rows[0][0]),
            context_floor=None if floor is None else str(floor),
        )

    def list_facts(self) -> tuple[MemoryFact, ...]:
        rows = self._storage.query(
            f"SELECT {FACT_COLUMNS} FROM memory_facts WHERE identity_id = ? "
            "ORDER BY CASE category WHEN 'preferred_name' THEN 0 ELSE 1 END, "
            "created_at, fact_id",
            (self._identity_id,),
            context="list memory facts",
        )
        return tuple(fact_from_row(row) for row in rows)

    def tombstones(self) -> dict[str, str]:
        rows = self._storage.query(
            "SELECT fact_id, deleted_at FROM memory_tombstones",
            context="read memory tombstones",
        )
        return {str(row[0]): str(row[1]) for row in rows}

    def resolve(self, ref: str) -> MemoryFact:
        """Find one fact by full id or an unambiguous prefix of 4+ characters."""
        needle = ref.strip().lower()
        if len(needle) < MIN_REF_CHARS:
            raise FactNotFoundError(
                f"Use at least {MIN_REF_CHARS} characters of a fact id "
                "(see /memory list)."
            )
        matches = [f for f in self.list_facts() if f.fact_id.startswith(needle)]
        if not matches:
            raise FactNotFoundError(f"No saved fact matches '{ref.strip()}'.")
        if len(matches) > 1:
            raise FactNotFoundError(
                f"'{ref.strip()}' matches more than one fact; use more characters."
            )
        return matches[0]

    def load_context(self) -> tuple[MemorySettings, ContextSelection]:
        """Read the live switch and facts for one turn (never cached)."""
        settings = self.settings()
        if not settings.enabled:
            return settings, ContextSelection(facts=(), omitted=0)
        return settings, select_context_facts(self.list_facts())

    # -- writes ------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self.settings().enabled:
            raise MemoryDisabledError(
                "Memory is off. Turn it on with /memory on before saving or "
                "correcting facts."
            )

    def save(self, category: Category, value: str) -> MemoryFact:
        """Save a confirmed fact; a preferred name replaces the previous one."""
        clean = validate_fact_value(value)
        self._require_enabled()
        now = utc_now_iso()
        with self._storage.transaction("save memory fact") as conn:
            existing = conn.execute(
                f"SELECT {FACT_COLUMNS} FROM memory_facts WHERE identity_id = ?",
                (self._identity_id,),
            ).fetchall()
            facts = [fact_from_row(tuple(r)) for r in existing]
            if category == "preferred_name":
                current = next((f for f in facts if f.category == category), None)
                if current is not None:
                    conn.execute(
                        "UPDATE memory_facts SET value = ?, updated_at = ?, "
                        "confirmed_at = ?, correction_count = correction_count + 1 "
                        "WHERE fact_id = ?",
                        (clean, now, now, current.fact_id),
                    )
                    self._raise_floor(conn, now)
                    return self._get(conn, current.fact_id)
            elif any(f.value == clean for f in facts if f.category == category):
                raise InvalidFactError("That preference is already saved.")
            if len(facts) >= MAX_FACTS:
                raise FactLimitError(
                    f"At most {MAX_FACTS} facts can be saved; delete one first."
                )
            fact_id = str(uuid.uuid4())
            conn.execute(
                f"INSERT INTO memory_facts ({FACT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'normal', 'until_deleted', "
                "NULL, NULL)",
                (
                    fact_id,
                    self._identity_id,
                    category,
                    clean,
                    SOURCE_EXPLICIT,
                    CONFIRMATION_LOCAL,
                    now,
                    now,
                    now,
                ),
            )
            return self._get(conn, fact_id)

    def correct(self, fact_id: str, value: str) -> MemoryFact:
        """Replace a fact's value after the user confirmed the correction."""
        clean = validate_fact_value(value)
        self._require_enabled()
        now = utc_now_iso()
        with self._storage.transaction("correct memory fact") as conn:
            self._get(conn, fact_id)
            conn.execute(
                "UPDATE memory_facts SET value = ?, updated_at = ?, confirmed_at = ?, "
                "correction_count = correction_count + 1 WHERE fact_id = ?",
                (clean, now, now, fact_id),
            )
            self._raise_floor(conn, now)
            return self._get(conn, fact_id)

    def delete(self, fact_id: str) -> None:
        """Delete one fact and keep only an id-only tombstone."""
        now = utc_now_iso()
        with self._storage.transaction("delete memory fact") as conn:
            self._get(conn, fact_id)
            conn.execute("DELETE FROM memory_facts WHERE fact_id = ?", (fact_id,))
            conn.execute(
                "INSERT OR REPLACE INTO memory_tombstones (fact_id, deleted_at) "
                "VALUES (?, ?)",
                (fact_id, now),
            )
            self._raise_floor(conn, now)

    def reset(self) -> int:
        """Delete every fact (memory switch and chat history are untouched)."""
        now = utc_now_iso()
        with self._storage.transaction("reset memory") as conn:
            rows = conn.execute(
                "SELECT fact_id FROM memory_facts WHERE identity_id = ?",
                (self._identity_id,),
            ).fetchall()
            for (fact_id,) in rows:
                conn.execute("DELETE FROM memory_facts WHERE fact_id = ?", (fact_id,))
                conn.execute(
                    "INSERT OR REPLACE INTO memory_tombstones (fact_id, deleted_at) "
                    "VALUES (?, ?)",
                    (fact_id, now),
                )
            if rows:
                self._raise_floor(conn, now)
            return len(rows)

    def set_enabled(self, enabled: bool) -> None:
        """Switch memory use on or off; stored facts are kept either way."""
        now = utc_now_iso()
        with self._storage.transaction("set memory switch") as conn:
            conn.execute(
                "UPDATE memory_settings SET enabled = ?, updated_at = ?",
                (1 if enabled else 0, now),
            )
            if not enabled:
                self._raise_floor(conn, now)

    def _raise_floor(self, conn: sqlite3.Connection, now: str) -> None:
        # Old transcripts must not silently repopulate context after a change.
        conn.execute(
            "UPDATE memory_settings SET context_floor = ?, updated_at = ?", (now, now)
        )

    def _get(self, conn: sqlite3.Connection, fact_id: str) -> MemoryFact:
        row = conn.execute(
            f"SELECT {FACT_COLUMNS} FROM memory_facts "
            "WHERE fact_id = ? AND identity_id = ?",
            (fact_id, self._identity_id),
        ).fetchone()
        if row is None:
            raise FactNotFoundError("That fact no longer exists.")
        return fact_from_row(tuple(row))


def select_context_facts(facts: tuple[MemoryFact, ...]) -> ContextSelection:
    """Pick a bounded, deterministic set of facts for one request.

    The preferred name comes first, then preferences by most recent
    confirmation. Selection stops at ``MAX_CONTEXT_FACTS`` facts or
    ``MAX_CONTEXT_CHARS`` total characters, whichever is hit first.
    """
    names = [f for f in facts if f.category == "preferred_name"]
    prefs = sorted(
        (f for f in facts if f.category == "preference"),
        key=lambda f: (f.confirmed_at, f.fact_id),
        reverse=True,
    )
    chosen: list[ContextFact] = []
    used = 0
    for fact in (*names, *prefs):
        if (
            len(chosen) >= MAX_CONTEXT_FACTS
            or used + len(fact.value) > MAX_CONTEXT_CHARS
        ):
            continue
        chosen.append(ContextFact(fact.fact_id, fact.category, fact.value))
        used += len(fact.value)
    return ContextSelection(facts=tuple(chosen), omitted=len(facts) - len(chosen))
