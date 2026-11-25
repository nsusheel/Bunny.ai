"""SQLite local state repository with transactional safety and concurrency control."""

from __future__ import annotations

import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
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
    DatabaseLockedError,
    IncompatibleSchemaError,
    MigrationError,
    MissingIdentityError,
    NewerSchemaError,
    RecoveryRequiredError,
    StorageError,
)
from bunny_ai.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS

SQLITE_HEADER = b"SQLite format 3\x00"
DB_FILENAME = "bunny.sqlite3"
LOCK_FILENAME = ".bunny.lock"
INIT_STAGING_PREFIX = f".{DB_FILENAME}.init-"
_INIT_STAGING_FILENAME_RE = re.compile(
    rf"{re.escape(INIT_STAGING_PREFIX)}[0-9a-f]{{8}}\.tmp(?:-journal)?",
    re.ASCII,
)

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


def _journal_path(db_path: Path) -> Path:
    return db_path.parent / f"{db_path.name}-journal"


def _has_hot_journal(db_path: Path) -> bool:
    journal = _journal_path(db_path)
    return journal.is_file() and journal.stat().st_size > 0


def _translate_sqlite_error(exc: sqlite3.Error, context: str) -> StorageError:
    if isinstance(exc, sqlite3.OperationalError) and _is_lock_error(exc):
        return DatabaseLockedError(
            f"Database is currently locked by another process ({context}). "
            "Wait for the other process to finish and retry."
        )
    return StorageError(f"SQLite storage failure ({context}): {exc}")


def _open_rw(db_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    deadline = time.monotonic() + (busy_timeout_ms / 1000.0)
    while True:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                str(db_path),
                timeout=busy_timeout_ms / 1000.0,
                isolation_level=None,
            )
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode = DELETE")
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            if _is_lock_error(exc):
                if time.monotonic() < deadline:
                    time.sleep(0.01 + random.uniform(0.0, 0.02))
                    continue
                raise DatabaseLockedError(
                    f"Database '{db_path}' is currently locked by another process. "
                    "Wait for the other process to finish and retry."
                ) from exc
            raise StorageError(f"Failed to open database '{db_path}': {exc}") from exc


def _open_ro(db_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    deadline = time.monotonic() + (busy_timeout_ms / 1000.0)
    while True:
        conn: sqlite3.Connection | None = None
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
            if conn is not None:
                conn.close()
            if _is_lock_error(exc):
                if time.monotonic() < deadline:
                    time.sleep(0.01 + random.uniform(0.0, 0.02))
                    continue
                raise DatabaseLockedError(
                    f"Database '{db_path}' is currently locked by another process. "
                    "Wait for the other process to finish and retry."
                ) from exc
            raise


def _begin_immediate(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    deadline = time.monotonic() + (busy_timeout_ms / 1000.0)
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc):
                if time.monotonic() < deadline:
                    time.sleep(0.01 + random.uniform(0.0, 0.02))
                    continue
                raise DatabaseLockedError(
                    "Database is currently locked by another process. "
                    "Wait for the other process to finish and retry."
                ) from exc
            raise StorageError(f"Failed to begin transaction: {exc}") from exc


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
                    raise DatabaseLockedError(
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
        DatabaseLockedError,
        NewerSchemaError,
        IncompatibleSchemaError,
        MissingIdentityError,
        RecoveryRequiredError,
        StorageError,
    ):
        raise
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            raise DatabaseLockedError(
                f"Database '{db_path}' is currently locked by another process. "
                "Wait for the other process to finish and retry."
            ) from exc
        if "readonly" in str(exc).lower() and _has_hot_journal(db_path):
            journal = _journal_path(db_path)
            raise RecoveryRequiredError(
                f"Database '{db_path}' has an unrecovered SQLite rollback journal "
                f"('{journal.name}') from an interrupted write. Read-only inspection "
                "cannot recover it; run `bunny chat` on this profile to recover "
                "the last committed state (do not delete the journal file manually)."
            ) from exc
        raise CorruptDatabaseError(
            f"Database '{db_path}' is corrupt or unreadable: {exc}. "
            "Existing file was preserved."
        ) from exc
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

    def _cleanup_stale_init_files(self) -> None:
        for candidate in self.data_dir.iterdir():
            if (
                _INIT_STAGING_FILENAME_RE.fullmatch(candidate.name) is not None
                and not candidate.is_symlink()
                and candidate.is_file()
            ):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    def _recover_hot_journal_if_valid(self) -> tuple[int, IdentityRecord]:
        journal = _journal_path(self.db_path)
        with tempfile.TemporaryDirectory(prefix="bunny-recover-") as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            staged_db = temp_dir / DB_FILENAME
            shutil.copy2(self.db_path, staged_db)
            if journal.is_file():
                shutil.copy2(journal, temp_dir / journal.name)

            staged_conn: sqlite3.Connection | None = None
            try:
                staged_conn = _open_rw(staged_db, self.busy_timeout_ms)
                staged_check = staged_conn.execute("PRAGMA integrity_check").fetchall()
                if staged_check != [("ok",)]:
                    raise CorruptDatabaseError(
                        f"Database '{self.db_path}' failed integrity check during "
                        f"staged journal recovery: {staged_check}. "
                        "Existing file was preserved."
                    )
            except sqlite3.DatabaseError as exc:
                raise CorruptDatabaseError(
                    f"Database '{self.db_path}' is corrupt or unreadable: {exc}. "
                    "Existing file was preserved."
                ) from exc
            finally:
                if staged_conn is not None:
                    staged_conn.close()

            # Validate staged recovered database before touching the real files.
            _validate_existing_ro(staged_db, self.busy_timeout_ms)

        rw_conn: sqlite3.Connection | None = None
        try:
            rw_conn = _open_rw(self.db_path, self.busy_timeout_ms)
            real_check = rw_conn.execute("PRAGMA integrity_check").fetchall()
            if real_check != [("ok",)]:
                raise CorruptDatabaseError(
                    f"Database '{self.db_path}' failed integrity check after "
                    f"journal recovery: {real_check}."
                )
        except sqlite3.DatabaseError as exc:
            raise CorruptDatabaseError(
                f"Database '{self.db_path}' failed journal recovery: {exc}."
            ) from exc
        finally:
            if rw_conn is not None:
                rw_conn.close()

        restrict_path_permissions(self.db_path, is_dir=False)
        schema_version, identity, _, _ = _validate_existing_ro(
            self.db_path, self.busy_timeout_ms
        )
        return schema_version, identity

    def _initialize_fresh(
        self, *, seed_version: str, style_version: str
    ) -> tuple[IdentityRecord, bool]:
        staging_path = (
            self.data_dir / f"{INIT_STAGING_PREFIX}{uuid.uuid4().hex[:8]}.tmp"
        )
        staging_conn: sqlite3.Connection | None = None
        try:
            staging_conn = _open_rw(staging_path, self.busy_timeout_ms)
            _begin_immediate(staging_conn, self.busy_timeout_ms)
            now = _utc_now_iso()
            for migration in MIGRATIONS:
                migration.apply(staging_conn)
                staging_conn.execute(
                    """
                    INSERT INTO schema_migrations (version, applied_at, description)
                    VALUES (?, ?, ?)
                    """,
                    (migration.version, now, migration.description),
                )

            identity_id = str(uuid.uuid4())
            staging_conn.execute(
                """
                INSERT INTO identity (
                    singleton, identity_id, seed_version, style_version,
                    created_at, awakened_at, onboarding_state, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, 'greeted', ?)
                """,
                (identity_id, seed_version, style_version, now, now, now),
            )
            staging_conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            staging_conn.execute("COMMIT")

            check = staging_conn.execute("PRAGMA integrity_check").fetchall()
            if check != [("ok",)]:
                raise StorageError(
                    f"Newly initialized database failed integrity check: {check}"
                )
            staging_conn.close()
            staging_conn = None

            restrict_path_permissions(staging_path, is_dir=False)
            if self.db_path.exists():
                raise StorageError(
                    f"Database '{self.db_path}' appeared unexpectedly during initialization."
                )
            staging_path.rename(self.db_path)
            restrict_path_permissions(self.db_path, is_dir=False)
            self._conn = _open_rw(self.db_path, self.busy_timeout_ms)
            identity = IdentityRecord(
                identity_id=identity_id,
                seed_version=seed_version,
                style_version=style_version,
                created_at=now,
                awakened_at=now,
                onboarding_state="greeted",
                updated_at=now,
            )
            return identity, True
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"Failed to initialize local database: {exc}") from exc
        finally:
            if staging_conn is not None:
                try:
                    if staging_conn.in_transaction:
                        staging_conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                staging_conn.close()
            for leftover in (staging_path, _journal_path(staging_path)):
                if leftover.exists():
                    try:
                        leftover.unlink()
                    except OSError:
                        pass

    def initialize_or_load(
        self, *, seed_version: str, style_version: str
    ) -> InitResult:
        """Validate, recover/migrate if needed, or atomically initialize the local identity."""
        with _acquire_init_lock(self.data_dir, self.busy_timeout_ms):
            if self.db_path.exists():
                try:
                    schema_version, identity, _, _ = _validate_existing_ro(
                        self.db_path, self.busy_timeout_ms
                    )
                except RecoveryRequiredError:
                    schema_version, identity = self._recover_hot_journal_if_valid()
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

            self._cleanup_stale_init_files()
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
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _translate_sqlite_error(exc, "update onboarding state") from exc
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
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _translate_sqlite_error(exc, "start history session") from exc
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
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _translate_sqlite_error(exc, "record turn") from exc
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
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _translate_sqlite_error(exc, "end history session") from exc
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
        try:
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
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc, "load recent turns") from exc
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
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _translate_sqlite_error(exc, "prune expired history") from exc
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
