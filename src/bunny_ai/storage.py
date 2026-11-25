"""SQLite local state repository with transactional safety and concurrency control."""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from bunny_ai.config import ensure_data_dir, restrict_path_permissions
from bunny_ai.errors import (
    CorruptDatabaseError,
    IncompatibleSchemaError,
    MigrationError,
    MissingIdentityError,
    NewerSchemaError,
    StorageError,
)
from bunny_ai.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS

SQLITE_HEADER = b"SQLite format 3\x00"
DB_FILENAME = "bunny.sqlite3"
LOCK_FILENAME = ".bunny.lock"

_REQUIRED_TABLES_BY_VERSION: dict[int, frozenset[str]] = {
    1: frozenset({"schema_migrations", "identity"}),
    2: frozenset({"schema_migrations", "identity", "sessions", "turns"}),
}

OnboardingState = Literal["greeted", "answered", "skipped"]


@dataclass(frozen=True)
class IdentityRecord:
    """Persisted Bunny identity record."""

    identity_id: str
    seed_version: str
    style_version: str
    created_at: str
    awakened_at: str
    onboarding_state: OnboardingState
    updated_at: str


@dataclass(frozen=True)
class TurnRecord:
    """Persisted local conversation turn pair."""

    turn_id: str
    session_id: str
    turn_index: int
    user_text: str
    assistant_text: str
    provider_mode: str
    created_at: str


@dataclass(frozen=True)
class InitResult:
    """Result of initializing or loading local state."""

    identity: IdentityRecord
    is_first_launch: bool
    schema_version: int
    backup_path: Path | None = None


@dataclass(frozen=True)
class StorageSnapshot:
    """Read-only status snapshot of the local database."""

    exists: bool
    db_path: Path
    schema_version: int | None = None
    identity: IdentityRecord | None = None
    session_count: int = 0
    turn_count: int = 0


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _open_rw(db_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000.0,
        isolation_level=None,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def _open_ro(db_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    deadline = time.monotonic() + (busy_timeout_ms / 1000.0)
    while True:
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=busy_timeout_ms / 1000.0,
                isolation_level=None,
            )
            conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            return conn
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc) and time.monotonic() < deadline:
                time.sleep(0.01 + random.uniform(0.0, 0.02))
                continue
            raise


def _begin_immediate(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    deadline = time.monotonic() + (busy_timeout_ms / 1000.0)
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc) and time.monotonic() < deadline:
                time.sleep(0.01 + random.uniform(0.0, 0.02))
                continue
            raise


@contextmanager
def _acquire_init_lock(data_dir: Path, busy_timeout_ms: int) -> Iterator[None]:
    """Serialize fresh initialization and migration across concurrent processes."""
    ensure_data_dir(data_dir)
    lock_path = data_dir / LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    restrict_path_permissions(lock_path, is_dir=False)
    deadline = time.monotonic() + (busy_timeout_ms / 1000.0)
    locked = False
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise StorageError(
                        f"Timed out waiting for storage lock '{lock_path}': {exc}"
                    ) from exc
                time.sleep(0.01 + random.uniform(0.0, 0.02))
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _row_to_identity(row: tuple[object, ...]) -> IdentityRecord:
    state = str(row[5])
    if state not in ("greeted", "answered", "skipped"):
        raise CorruptDatabaseError(f"Unexpected onboarding_state value: {state!r}")
    return IdentityRecord(
        identity_id=str(row[0]),
        seed_version=str(row[1]),
        style_version=str(row[2]),
        created_at=str(row[3]),
        awakened_at=str(row[4]),
        onboarding_state=cast(OnboardingState, state),
        updated_at=str(row[6]),
    )


def _read_identity(conn: sqlite3.Connection) -> IdentityRecord:
    rows = conn.execute(
        """
        SELECT identity_id, seed_version, style_version, created_at,
               awakened_at, onboarding_state, updated_at
        FROM identity
        """
    ).fetchall()
    if len(rows) == 0:
        raise MissingIdentityError(
            "Database schema is initialized, but the required identity record is missing. "
            "Existing database was left untouched."
        )
    if len(rows) > 1:
        raise CorruptDatabaseError(
            f"Expected 1 identity record, found {len(rows)}. Existing database was left untouched."
        )
    return _row_to_identity(rows[0])


def _validate_existing_ro(
    db_path: Path, busy_timeout_ms: int
) -> tuple[int, IdentityRecord, int, int]:
    """Inspect an existing database strictly read-only without modifying it."""
    if not db_path.is_file():
        raise CorruptDatabaseError(f"Database path is not a regular file: {db_path}")
    try:
        size = db_path.stat().st_size
        if size < len(SQLITE_HEADER):
            raise CorruptDatabaseError(
                f"Database file '{db_path}' is truncated or empty ({size} bytes). "
                "Existing file was preserved."
            )
        with db_path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError as exc:
        raise StorageError(f"Cannot read database file '{db_path}': {exc}") from exc

    if header != SQLITE_HEADER:
        raise CorruptDatabaseError(
            f"Database file '{db_path}' does not have a valid SQLite header. "
            "Existing file was preserved."
        )

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_ro(db_path, busy_timeout_ms)
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        if integrity_rows != [("ok",)]:
            raise CorruptDatabaseError(
                f"Database file '{db_path}' failed integrity check: {integrity_rows}. "
                "Existing file was preserved."
            )

        version_row = conn.execute("PRAGMA user_version").fetchone()
        if version_row is None:
            raise CorruptDatabaseError(
                f"Could not read schema version from '{db_path}'."
            )
        schema_version = int(version_row[0])

        if schema_version > CURRENT_SCHEMA_VERSION:
            raise NewerSchemaError(
                f"Database schema version {schema_version} is newer than supported "
                f"version {CURRENT_SCHEMA_VERSION}. Existing database was preserved."
            )
        if schema_version < 0:
            raise CorruptDatabaseError(
                f"Invalid negative schema version {schema_version} in '{db_path}'."
            )
        if schema_version == 0:
            existing_objects = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
            if existing_objects:
                raise IncompatibleSchemaError(
                    f"Database '{db_path}' contains unversioned tables and is not a "
                    "recognized Bunny.ai schema. Existing file was preserved."
                )
            raise IncompatibleSchemaError(
                f"Database '{db_path}' has schema version 0 without Bunny.ai tables. "
                "Existing file was preserved."
            )

        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        table_names = {str(row[0]) for row in table_rows}
        expected_tables = _REQUIRED_TABLES_BY_VERSION.get(schema_version)
        if expected_tables is None:
            raise IncompatibleSchemaError(
                f"Unsupported schema version {schema_version} in '{db_path}'."
            )
        missing_tables = sorted(expected_tables - table_names)
        if missing_tables:
            raise CorruptDatabaseError(
                f"Database '{db_path}' (schema v{schema_version}) is missing required "
                f"table(s): {', '.join(missing_tables)}. Existing file was preserved."
            )

        applied_rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied_versions = [int(row[0]) for row in applied_rows]
        if applied_versions != list(range(1, schema_version + 1)):
            raise CorruptDatabaseError(
                f"Database '{db_path}' has inconsistent migration history "
                f"{applied_versions} for schema v{schema_version}."
            )

        identity = _read_identity(conn)
        session_count = 0
        turn_count = 0
        if schema_version >= 2:
            session_row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            turn_row = conn.execute("SELECT COUNT(*) FROM turns").fetchone()
            session_count = int(session_row[0]) if session_row is not None else 0
            turn_count = int(turn_row[0]) if turn_row is not None else 0
        return schema_version, identity, session_count, turn_count
    except (
        CorruptDatabaseError,
        NewerSchemaError,
        IncompatibleSchemaError,
        MissingIdentityError,
        StorageError,
    ):
        raise
    except sqlite3.DatabaseError as exc:
        raise CorruptDatabaseError(
            f"Database '{db_path}' is corrupt or unreadable: {exc}. "
            "Existing file was preserved."
        ) from exc
    finally:
        if conn is not None:
            conn.close()


class LocalStorage:
    """Manages Bunny.ai's local SQLite state."""

    def __init__(self, data_dir: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.data_dir = data_dir
        self.db_path = data_dir / DB_FILENAME
        self.busy_timeout_ms = busy_timeout_ms
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> LocalStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close any open SQLite connection cleanly."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("Local storage is not open.")
        return self._conn

    def inspect(self) -> StorageSnapshot:
        """Inspect local database state read-only without creating files."""
        if not self.db_path.exists():
            return StorageSnapshot(exists=False, db_path=self.db_path)
        schema_version, identity, session_count, turn_count = _validate_existing_ro(
            self.db_path, self.busy_timeout_ms
        )
        return StorageSnapshot(
            exists=True,
            db_path=self.db_path,
            schema_version=schema_version,
            identity=identity,
            session_count=session_count,
            turn_count=turn_count,
        )

    def _create_verified_backup(self, target_version: int) -> Path:
        backups_dir = ensure_data_dir(self.data_dir / "backups")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = (
            backups_dir
            / f"bunny-pre-v{target_version}-{timestamp}-{uuid.uuid4().hex[:8]}.sqlite3"
        )
        src_conn: sqlite3.Connection | None = None
        dst_conn: sqlite3.Connection | None = None
        verify_conn: sqlite3.Connection | None = None
        try:
            src_conn = _open_ro(self.db_path, self.busy_timeout_ms)
            dst_conn = sqlite3.connect(str(backup_path), isolation_level=None)
            src_conn.backup(dst_conn)
            dst_conn.close()
            dst_conn = None
            src_conn.close()
            src_conn = None

            restrict_path_permissions(backup_path, is_dir=False)
            verify_conn = _open_ro(backup_path, self.busy_timeout_ms)
            check = verify_conn.execute("PRAGMA integrity_check").fetchall()
            if check != [("ok",)]:
                raise MigrationError(
                    f"Preflight backup '{backup_path}' failed integrity check."
                )
            return backup_path
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                f"Failed to create verified preflight backup: {exc}"
            ) from exc
        finally:
            for conn in (verify_conn, dst_conn, src_conn):
                if conn is not None:
                    conn.close()

    def _migrate_existing(self, current_version: int) -> Path | None:
        backup_path = self._create_verified_backup(CURRENT_SCHEMA_VERSION)
        conn = _open_rw(self.db_path, self.busy_timeout_ms)
        try:
            _begin_immediate(conn, self.busy_timeout_ms)
            actual_row = conn.execute("PRAGMA user_version").fetchone()
            actual_version = (
                int(actual_row[0]) if actual_row is not None else current_version
            )
            applied_at = _utc_now_iso()
            for migration in MIGRATIONS:
                if migration.version > actual_version:
                    migration.apply(conn)
                    conn.execute(
                        """
                        INSERT INTO schema_migrations (version, applied_at, description)
                        VALUES (?, ?, ?)
                        """,
                        (migration.version, applied_at, migration.description),
                    )
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            conn.execute("COMMIT")
            self._conn = conn
            restrict_path_permissions(self.db_path, is_dir=False)
            return backup_path
        except Exception as exc:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()
            raise MigrationError(
                f"Schema migration failed (backup preserved at '{backup_path}'): {exc}"
            ) from exc

    def _initialize_fresh(
        self, *, seed_version: str, style_version: str
    ) -> tuple[IdentityRecord, bool]:
        conn = _open_rw(self.db_path, self.busy_timeout_ms)
        try:
            _begin_immediate(conn, self.busy_timeout_ms)
            version_row = conn.execute("PRAGMA user_version").fetchone()
            actual_version = int(version_row[0]) if version_row is not None else 0
            if actual_version == CURRENT_SCHEMA_VERSION:
                identity = _read_identity(conn)
                conn.execute("COMMIT")
                self._conn = conn
                return identity, False

            if actual_version != 0:
                conn.execute("ROLLBACK")
                conn.close()
                raise IncompatibleSchemaError(
                    f"Unexpected schema version {actual_version} during initialization."
                )

            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if existing:
                conn.execute("ROLLBACK")
                conn.close()
                raise IncompatibleSchemaError(
                    "Existing unversioned tables found during initialization."
                )

            now = _utc_now_iso()
            for migration in MIGRATIONS:
                migration.apply(conn)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (version, applied_at, description)
                    VALUES (?, ?, ?)
                    """,
                    (migration.version, now, migration.description),
                )

            identity_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO identity (
                    singleton, identity_id, seed_version, style_version,
                    created_at, awakened_at, onboarding_state, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, 'greeted', ?)
                """,
                (identity_id, seed_version, style_version, now, now, now),
            )
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            conn.execute("COMMIT")
            restrict_path_permissions(self.db_path, is_dir=False)
            identity = IdentityRecord(
                identity_id=identity_id,
                seed_version=seed_version,
                style_version=style_version,
                created_at=now,
                awakened_at=now,
                onboarding_state="greeted",
                updated_at=now,
            )
            self._conn = conn
            return identity, True
        except (
            CorruptDatabaseError,
            IncompatibleSchemaError,
            MissingIdentityError,
            NewerSchemaError,
            MigrationError,
            StorageError,
        ):
            raise
        except Exception as exc:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()
            raise StorageError(f"Failed to initialize local database: {exc}") from exc

    def initialize_or_load(
        self, *, seed_version: str, style_version: str
    ) -> InitResult:
        """Validate, migrate if needed, or atomically initialize the local identity."""
        with _acquire_init_lock(self.data_dir, self.busy_timeout_ms):
            if self.db_path.exists():
                schema_version, identity, _, _ = _validate_existing_ro(
                    self.db_path, self.busy_timeout_ms
                )
                backup_path: Path | None = None
                if schema_version < CURRENT_SCHEMA_VERSION:
                    backup_path = self._migrate_existing(schema_version)
                    identity = _read_identity(self._require_conn())
                else:
                    self._conn = _open_rw(self.db_path, self.busy_timeout_ms)
                return InitResult(
                    identity=identity,
                    is_first_launch=False,
                    schema_version=CURRENT_SCHEMA_VERSION,
                    backup_path=backup_path,
                )

            identity, is_first_launch = self._initialize_fresh(
                seed_version=seed_version,
                style_version=style_version,
            )
            return InitResult(
                identity=identity,
                is_first_launch=is_first_launch,
                schema_version=CURRENT_SCHEMA_VERSION,
                backup_path=None,
            )

    def update_onboarding_state(
        self, identity_id: str, state: Literal["answered", "skipped"]
    ) -> IdentityRecord:
        """Persist onboarding transition ('answered' or 'skipped')."""
        conn = self._require_conn()
        now = _utc_now_iso()
        _begin_immediate(conn, self.busy_timeout_ms)
        try:
            conn.execute(
                """
                UPDATE identity
                SET onboarding_state = ?, updated_at = ?
                WHERE identity_id = ?
                """,
                (state, now, identity_id),
            )
            identity = _read_identity(conn)
            conn.execute("COMMIT")
            return identity
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def start_history_session(self, identity_id: str) -> str:
        """Create an opt-in local history session row."""
        conn = self._require_conn()
        session_id = str(uuid.uuid4())
        now = _utc_now_iso()
        _begin_immediate(conn, self.busy_timeout_ms)
        try:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, identity_id, started_at, ended_at, history_enabled
                )
                VALUES (?, ?, ?, NULL, 1)
                """,
                (session_id, identity_id, now),
            )
            conn.execute("COMMIT")
            return session_id
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def record_turn(
        self,
        *,
        session_id: str,
        turn_index: int,
        user_text: str,
        assistant_text: str,
        provider_mode: str,
    ) -> TurnRecord:
        """Persist one completed user/assistant turn pair in an opt-in session."""
        conn = self._require_conn()
        turn_id = str(uuid.uuid4())
        now = _utc_now_iso()
        _begin_immediate(conn, self.busy_timeout_ms)
        try:
            conn.execute(
                """
                INSERT INTO turns (
                    turn_id, session_id, turn_index, user_text,
                    assistant_text, provider_mode, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    turn_index,
                    user_text,
                    assistant_text,
                    provider_mode,
                    now,
                ),
            )
            conn.execute("COMMIT")
            return TurnRecord(
                turn_id=turn_id,
                session_id=session_id,
                turn_index=turn_index,
                user_text=user_text,
                assistant_text=assistant_text,
                provider_mode=provider_mode,
                created_at=now,
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def end_history_session(self, session_id: str) -> None:
        """Record session end timestamp on clean shutdown."""
        conn = self._require_conn()
        now = _utc_now_iso()
        _begin_immediate(conn, self.busy_timeout_ms)
        try:
            conn.execute(
                """
                UPDATE sessions
                SET ended_at = COALESCE(ended_at, ?)
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def load_recent_turns(
        self, identity_id: str, *, limit: int, retention_days: int
    ) -> tuple[TurnRecord, ...]:
        """Load recent opt-in history turns within the retention window."""
        conn = self._require_conn()
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        rows = conn.execute(
            """
            SELECT t.turn_id, t.session_id, t.turn_index, t.user_text,
                   t.assistant_text, t.provider_mode, t.created_at
            FROM turns t
            JOIN sessions s ON s.session_id = t.session_id
            WHERE s.identity_id = ? AND s.history_enabled = 1 AND t.created_at >= ?
            ORDER BY t.created_at DESC, t.turn_index DESC
            LIMIT ?
            """,
            (identity_id, cutoff, limit),
        ).fetchall()
        records = [
            TurnRecord(
                turn_id=str(row[0]),
                session_id=str(row[1]),
                turn_index=int(row[2]),
                user_text=str(row[3]),
                assistant_text=str(row[4]),
                provider_mode=str(row[5]),
                created_at=str(row[6]),
            )
            for row in reversed(rows)
        ]
        return tuple(records)

    def prune_expired_history(
        self,
        identity_id: str,
        *,
        retention_days: int,
        now: datetime | None = None,
    ) -> int:
        """Delete stored turns older than retention_days inside a transaction."""
        conn = self._require_conn()
        reference = now if now is not None else datetime.now(UTC)
        cutoff = (reference - timedelta(days=retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        _begin_immediate(conn, self.busy_timeout_ms)
        try:
            cursor = conn.execute(
                """
                DELETE FROM turns
                WHERE created_at < ?
                  AND session_id IN (
                      SELECT session_id FROM sessions WHERE identity_id = ?
                  )
                """,
                (cutoff, identity_id),
            )
            deleted_turns = int(cursor.rowcount) if cursor.rowcount >= 0 else 0
            conn.execute(
                """
                DELETE FROM sessions
                WHERE identity_id = ?
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM turns WHERE turns.session_id = sessions.session_id
                  )
                """,
                (identity_id, cutoff),
            )
            conn.execute("COMMIT")
            return deleted_turns
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
