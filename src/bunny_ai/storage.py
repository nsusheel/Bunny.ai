"""SQLite local state repository with transactional safety and concurrency control."""

from __future__ import annotations

import ctypes
import errno
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from bunny_ai._watermark import (
    LEDGER_LOCK_FILENAME,
    LedgerWatermark,
    has_stale_rollback,
    merge_watermarks,
    read_db_ledger_state,
    read_watermark,
    reconcile_db_with_watermark,
    watermark_path,
    write_watermark_atomic,
)
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
SESSION_LOCK_FILENAME = ".bunny.session.lock"
_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2
_ERROR_LOCK_VIOLATION = 33
_RECOVERABLE_DAMAGE_ERRORS = (
    CorruptDatabaseError,
    MissingIdentityError,
    RecoveryRequiredError,
)
INIT_STAGING_PREFIX = f".{DB_FILENAME}.init-"
_INIT_STAGING_FILENAME_RE = re.compile(
    rf"{re.escape(INIT_STAGING_PREFIX)}[0-9a-f]{{8}}\.tmp(?:-journal)?",
    re.ASCII,
)

_REQUIRED_TABLES_BY_VERSION: dict[int, frozenset[str]] = {
    1: frozenset({"schema_migrations", "identity"}),
    2: frozenset({"schema_migrations", "identity", "sessions", "turns"}),
    3: frozenset(
        {
            "schema_migrations",
            "identity",
            "sessions",
            "turns",
            "memory_settings",
            "memory_facts",
            "memory_tombstones",
        }
    ),
    4: frozenset(
        {
            "schema_migrations",
            "identity",
            "sessions",
            "turns",
            "memory_settings",
            "memory_facts",
            "memory_tombstones",
            "budget_policy",
            "gateway_attempts",
            "gateway_receipts",
        }
    ),
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
    fact_count: int = 0
    memory_enabled: bool | None = None
    per_request_micro_usd: int | None = None
    cumulative_micro_usd: int | None = None
    settled_micro_usd: int = 0
    held_micro_usd: int = 0
    available_micro_usd: int | None = None
    dispatch_blocked: bool = False
    block_reason: str | None = None
    attempt_count: int = 0
    uncertain_hold_count: int = 0


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of restoring a local backup over the profile database."""

    restored_from: Path
    safety_copy: Path | None
    identity: IdentityRecord
    schema_version: int
    ledger_reconciled_attempts: int = 0
    dispatch_blocked: bool = False


@dataclass(frozen=True)
class BackupInfo:
    """Read-only description of one file in the profile ``backups`` folder."""

    path: Path
    schema_version: int | None
    identity_id: str | None
    turn_count: int
    fact_count: int
    problem: str | None = None


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
def acquire_exclusive_file_lock(
    lock_path: Path, busy_timeout_ms: int
) -> Iterator[None]:
    """Acquire an exclusive OS file lock on ``lock_path`` with bounded retry."""
    ensure_data_dir(lock_path.parent)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + (busy_timeout_ms / 1000.0)
    locked = False
    try:
        restrict_path_permissions(lock_path, is_dir=False)
        while True:
            if _try_lock_fd(fd, exclusive=True):
                locked = True
                break
            if time.monotonic() >= deadline:
                raise DatabaseLockedError(
                    f"Timed out waiting for storage lock '{lock_path}'."
                )
            time.sleep(0.01 + random.uniform(0.0, 0.02))
        yield
    finally:
        if locked:
            _unlock_fd(fd)
        else:
            os.close(fd)


@contextmanager
def _acquire_init_lock(data_dir: Path, busy_timeout_ms: int) -> Iterator[None]:
    """Serialize fresh initialization and migration across concurrent processes."""
    with acquire_exclusive_file_lock(data_dir / LOCK_FILENAME, busy_timeout_ms):
        yield


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


if sys.platform == "win32":

    def _win32_kernel32() -> ctypes.WinDLL:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LockFileEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_OVERLAPPED),
        ]
        kernel32.LockFileEx.restype = ctypes.c_int
        kernel32.UnlockFileEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_OVERLAPPED),
        ]
        kernel32.UnlockFileEx.restype = ctypes.c_int
        return kernel32


def _try_lock_fd(fd: int, *, exclusive: bool) -> bool:
    """Attempt a non-blocking shared or exclusive OS lock on ``fd``."""
    if sys.platform == "win32":
        import msvcrt

        flags = _LOCKFILE_FAIL_IMMEDIATELY | (
            _LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
        )
        overlapped = _OVERLAPPED()
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(fd))
        if _win32_kernel32().LockFileEx(
            handle, flags, 0, 1, 0, ctypes.byref(overlapped)
        ):
            return True
        err = ctypes.get_last_error()
        if err == _ERROR_LOCK_VIOLATION:
            return False
        raise ctypes.WinError(err)
    import fcntl

    mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    try:
        fcntl.flock(fd, mode)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        raise


def _unlock_fd(fd: int) -> None:
    """Release an OS lock on ``fd`` and close the descriptor."""
    try:
        if sys.platform == "win32":
            import msvcrt

            overlapped = _OVERLAPPED()
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(fd))
            _win32_kernel32().UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
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


def inspect_file(db_path: Path, busy_timeout_ms: int) -> StorageSnapshot:
    """Validate one database file read-only and describe it."""
    schema_version, identity, session_count, turn_count = _validate_existing_ro(
        db_path, busy_timeout_ms
    )
    fact_count = 0
    memory_enabled: bool | None = None
    per_request_micro_usd: int | None = None
    cumulative_micro_usd: int | None = None
    settled_micro_usd = 0
    held_micro_usd = 0
    available_micro_usd: int | None = None
    dispatch_blocked = False
    block_reason: str | None = None
    attempt_count = 0
    uncertain_hold_count = 0

    if schema_version >= 3:
        conn = _open_ro(db_path, busy_timeout_ms)
        try:
            fact_count = int(
                conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0]
            )
            row = conn.execute("SELECT enabled FROM memory_settings").fetchone()
            memory_enabled = bool(row[0]) if row is not None else None
            if schema_version >= 4:
                policy = conn.execute(
                    """
                    SELECT per_request_micro_usd, cumulative_micro_usd,
                           dispatch_blocked, block_reason
                    FROM budget_policy WHERE singleton = 1
                    """
                ).fetchone()
                if policy is None:
                    raise CorruptDatabaseError(
                        f"Database '{db_path}' is missing budget_policy row."
                    )
                per_request_micro_usd = None if policy[0] is None else int(policy[0])
                cumulative_micro_usd = None if policy[1] is None else int(policy[1])
                dispatch_blocked = bool(policy[2])
                block_reason = None if policy[3] is None else str(policy[3])
                agg = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(settled_micro_usd), 0),
                        COALESCE(SUM(held_micro_usd), 0),
                        COUNT(*),
                        COALESCE(SUM(CASE WHEN state = 'uncertain_hold' THEN 1 ELSE 0 END), 0)
                    FROM gateway_attempts
                    WHERE identity_id = ?
                    """,
                    (identity.identity_id,),
                ).fetchone()
                if agg is not None:
                    settled_micro_usd = int(agg[0])
                    held_micro_usd = int(agg[1])
                    attempt_count = int(agg[2])
                    uncertain_hold_count = int(agg[3])
                if cumulative_micro_usd is not None:
                    available_micro_usd = max(
                        0, cumulative_micro_usd - settled_micro_usd - held_micro_usd
                    )
        except sqlite3.Error as exc:
            raise CorruptDatabaseError(
                f"Database '{db_path}' metadata tables are unreadable: {exc}."
            ) from exc
        finally:
            conn.close()
    return StorageSnapshot(
        exists=True,
        db_path=db_path,
        schema_version=schema_version,
        identity=identity,
        session_count=session_count,
        turn_count=turn_count,
        fact_count=fact_count,
        memory_enabled=memory_enabled,
        per_request_micro_usd=per_request_micro_usd,
        cumulative_micro_usd=cumulative_micro_usd,
        settled_micro_usd=settled_micro_usd,
        held_micro_usd=held_micro_usd,
        available_micro_usd=available_micro_usd,
        dispatch_blocked=dispatch_blocked,
        block_reason=block_reason,
        attempt_count=attempt_count,
        uncertain_hold_count=uncertain_hold_count,
    )


class LocalStorage:
    """Manages Bunny.ai's local SQLite state."""

    def __init__(self, data_dir: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.data_dir = data_dir
        self.db_path = data_dir / DB_FILENAME
        self.busy_timeout_ms = busy_timeout_ms
        self._conn: sqlite3.Connection | None = None
        self._session_lock_fd: int | None = None

    def __enter__(self) -> LocalStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close any open SQLite connection cleanly and release the session lock."""
        try:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        finally:
            if self._session_lock_fd is not None:
                fd = self._session_lock_fd
                self._session_lock_fd = None
                _unlock_fd(fd)

    def _acquire_session_shared(self) -> None:
        """Acquire a shared session lock so restores refuse while a session is open."""
        if self._session_lock_fd is not None:
            return
        ensure_data_dir(self.data_dir)
        lock_path = self.data_dir / SESSION_LOCK_FILENAME
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            restrict_path_permissions(lock_path, is_dir=False)
            deadline = time.monotonic() + (self.busy_timeout_ms / 1000.0)
            while True:
                if _try_lock_fd(fd, exclusive=False):
                    locked = True
                    break
                if time.monotonic() >= deadline:
                    raise DatabaseLockedError(
                        f"Timed out waiting for session lock '{lock_path}'."
                    )
                time.sleep(0.01 + random.uniform(0.0, 0.02))
        except BaseException:
            if locked:
                _unlock_fd(fd)
            else:
                os.close(fd)
            raise
        self._session_lock_fd = fd

    def _finalize_session_open(self) -> None:
        try:
            self._acquire_session_shared()
        except BaseException:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            raise

    @contextmanager
    def _acquire_session_exclusive(self) -> Iterator[None]:
        """Acquire an exclusive session lock when a session lock file exists."""
        lock_path = self.data_dir / SESSION_LOCK_FILENAME
        if not lock_path.exists():
            yield
            return
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            restrict_path_permissions(lock_path, is_dir=False)
            timeout_ms = min(self.busy_timeout_ms, 200)
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            while True:
                if _try_lock_fd(fd, exclusive=True):
                    locked = True
                    break
                if time.monotonic() >= deadline:
                    raise DatabaseLockedError(
                        "Profile is currently open in another Bunny session. "
                        "Close active `bunny chat` sessions before restoring a backup."
                    )
                time.sleep(0.01 + random.uniform(0.0, 0.02))
            yield
        finally:
            if locked:
                _unlock_fd(fd)
            else:
                os.close(fd)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("Local storage is not open.")
        return self._conn

    def inspect(self) -> StorageSnapshot:
        """Inspect local database state read-only without creating files."""
        if not self.db_path.exists():
            return StorageSnapshot(exists=False, db_path=self.db_path)
        snapshot = inspect_file(self.db_path, self.busy_timeout_ms)
        if snapshot.identity is None or not watermark_path(self.data_dir).exists():
            return snapshot
        wm = read_watermark(self.data_dir)
        if wm is None:
            return snapshot
        if wm.identity_id != snapshot.identity.identity_id:
            return StorageSnapshot(
                exists=snapshot.exists,
                db_path=snapshot.db_path,
                schema_version=snapshot.schema_version,
                identity=snapshot.identity,
                session_count=snapshot.session_count,
                turn_count=snapshot.turn_count,
                fact_count=snapshot.fact_count,
                memory_enabled=snapshot.memory_enabled,
                per_request_micro_usd=snapshot.per_request_micro_usd,
                cumulative_micro_usd=snapshot.cumulative_micro_usd,
                settled_micro_usd=snapshot.settled_micro_usd,
                held_micro_usd=snapshot.held_micro_usd,
                available_micro_usd=snapshot.available_micro_usd,
                dispatch_blocked=True,
                block_reason="ledger_watermark_identity_mismatch",
                attempt_count=snapshot.attempt_count,
                uncertain_hold_count=snapshot.uncertain_hold_count,
            )

        if snapshot.schema_version is not None and snapshot.schema_version >= 4:
            conn = _open_ro(self.db_path, self.busy_timeout_ms)
            try:
                db_state = read_db_ledger_state(conn, snapshot.identity.identity_id)
            finally:
                conn.close()
            if not has_stale_rollback(db_state, wm):
                return snapshot
            merged = merge_watermarks(db_state, wm)
            settled = sum(a.settled_micro_usd for a in merged.attempts)
            held = sum(a.held_micro_usd for a in merged.attempts)
            uncertain = sum(1 for a in merged.attempts if a.state == "uncertain_hold")
            cum = snapshot.cumulative_micro_usd
            avail = max(0, cum - settled - held) if cum is not None else None
            return StorageSnapshot(
                exists=snapshot.exists,
                db_path=snapshot.db_path,
                schema_version=snapshot.schema_version,
                identity=snapshot.identity,
                session_count=snapshot.session_count,
                turn_count=snapshot.turn_count,
                fact_count=snapshot.fact_count,
                memory_enabled=snapshot.memory_enabled,
                per_request_micro_usd=snapshot.per_request_micro_usd,
                cumulative_micro_usd=cum,
                settled_micro_usd=settled,
                held_micro_usd=held,
                available_micro_usd=avail,
                dispatch_blocked=True,
                block_reason=wm.block_reason
                or "stale_backup_detected_reconcile_required",
                attempt_count=max(snapshot.attempt_count, len(merged.attempts)),
                uncertain_hold_count=uncertain,
            )

        if wm.highest_attempt_seq > 0 or wm.attempts or wm.dispatch_blocked:
            settled = sum(a.settled_micro_usd for a in wm.attempts)
            held = sum(a.held_micro_usd for a in wm.attempts)
            uncertain = sum(1 for a in wm.attempts if a.state == "uncertain_hold")
            cum = wm.cumulative_micro_usd
            avail = max(0, cum - settled - held) if cum is not None else None
            return StorageSnapshot(
                exists=snapshot.exists,
                db_path=snapshot.db_path,
                schema_version=snapshot.schema_version,
                identity=snapshot.identity,
                session_count=snapshot.session_count,
                turn_count=snapshot.turn_count,
                fact_count=snapshot.fact_count,
                memory_enabled=snapshot.memory_enabled,
                per_request_micro_usd=wm.per_request_micro_usd,
                cumulative_micro_usd=cum,
                settled_micro_usd=settled,
                held_micro_usd=held,
                available_micro_usd=avail,
                dispatch_blocked=True,
                block_reason=wm.block_reason
                or "stale_backup_detected_reconcile_required",
                attempt_count=len(wm.attempts),
                uncertain_hold_count=uncertain,
            )
        return snapshot

    @contextmanager
    def read_transaction(self, context: str) -> Iterator[sqlite3.Connection]:
        """Run one consistent ``BEGIN DEFERRED`` read snapshot."""
        conn = self._require_conn()
        if conn.in_transaction:
            # Already inside an outer transaction snapshot; reuse it cleanly.
            try:
                yield conn
            except sqlite3.Error as exc:
                raise _translate_sqlite_error(exc, context) from exc
            return
        deadline = time.monotonic() + (self.busy_timeout_ms / 1000.0)
        while True:
            try:
                conn.execute("BEGIN DEFERRED")
                break
            except sqlite3.OperationalError as exc:
                if _is_lock_error(exc):
                    if time.monotonic() < deadline:
                        time.sleep(0.01 + random.uniform(0.0, 0.02))
                        continue
                    raise DatabaseLockedError(
                        "Database is currently locked by another process. "
                        "Wait for the other process to finish and retry."
                    ) from exc
                raise StorageError(f"Failed to begin read transaction: {exc}") from exc
        try:
            yield conn
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc, context) from exc
        finally:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass

    @contextmanager
    def transaction(self, context: str) -> Iterator[sqlite3.Connection]:
        """Run one ``BEGIN IMMEDIATE`` transaction with rollback on any failure."""
        conn = self._require_conn()
        _begin_immediate(conn, self.busy_timeout_ms)
        try:
            yield conn
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _translate_sqlite_error(exc, context) from exc
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def query(
        self, sql: str, params: tuple[object, ...] = (), *, context: str = "query"
    ) -> list[tuple[object, ...]]:
        """Run one read statement outside an explicit transaction."""
        conn = self._require_conn()
        try:
            return [tuple(row) for row in conn.execute(sql, params).fetchall()]
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc, context) from exc

    def list_backups(self) -> tuple[BackupInfo, ...]:
        """Describe every ``*.sqlite3`` backup without modifying anything."""
        backups_dir = self.data_dir / "backups"
        if not backups_dir.is_dir():
            return ()
        infos: list[BackupInfo] = []
        for candidate in sorted(backups_dir.glob("*.sqlite3")):
            try:
                snap = inspect_file(candidate, self.busy_timeout_ms)
            except StorageError as exc:
                infos.append(BackupInfo(candidate, None, None, 0, 0, problem=str(exc)))
                continue
            infos.append(
                BackupInfo(
                    path=candidate,
                    schema_version=snap.schema_version,
                    identity_id=snap.identity.identity_id if snap.identity else None,
                    turn_count=snap.turn_count,
                    fact_count=snap.fact_count,
                )
            )
        return tuple(infos)

    def _merge_live_watermark(
        self, identity_id: str, live_schema_version: int | None
    ) -> LedgerWatermark | None:
        has_wm = watermark_path(self.data_dir).exists()
        has_live_v4 = (
            self.db_path.exists()
            and live_schema_version is not None
            and live_schema_version >= 4
        )
        if not has_wm and not has_live_v4:
            return None
        if not has_wm and has_live_v4:
            conn = _open_ro(self.db_path, self.busy_timeout_ms)
            try:
                live_state = read_db_ledger_state(conn, identity_id)
            finally:
                conn.close()
            if (
                not live_state.attempts
                and not live_state.dispatch_blocked
                and live_state.highest_attempt_seq == 0
            ):
                return None

        with acquire_exclusive_file_lock(
            self.data_dir / LEDGER_LOCK_FILENAME, self.busy_timeout_ms
        ):
            wm = read_watermark(self.data_dir)
            merged: LedgerWatermark | None = wm
            if has_live_v4:
                conn = _open_ro(self.db_path, self.busy_timeout_ms)
                try:
                    live_state = read_db_ledger_state(conn, identity_id)
                finally:
                    conn.close()
                if wm is not None:
                    merged = merge_watermarks(live_state, wm)
                elif (
                    live_state.attempts
                    or live_state.dispatch_blocked
                    or live_state.highest_attempt_seq > 0
                ):
                    merged = live_state
            if merged is not None:
                write_watermark_atomic(self.data_dir, merged)
            return merged

    def restore_from_backup(self, backup_path: Path) -> RestoreResult:
        """Replace the profile database with a validated backup.

        The current database (readable or not) is first preserved in
        ``backups/``. A backup that belongs to a different identity is refused.
        Any ledger attempts, holds, or blocks newer than the backup are carried
        forward and dispatch is blocked until reviewed.
        """
        with _acquire_init_lock(self.data_dir, self.busy_timeout_ms):
            with self._acquire_session_exclusive():
                snap = inspect_file(backup_path, self.busy_timeout_ms)
                if snap.identity is None or snap.schema_version is None:
                    raise StorageError(
                        f"Backup '{backup_path}' has no usable identity."
                    )
                current: StorageSnapshot | None = None
                current_damaged = False
                if self.db_path.exists():
                    try:
                        current = inspect_file(self.db_path, self.busy_timeout_ms)
                    except _RECOVERABLE_DAMAGE_ERRORS:
                        current = None
                        current_damaged = True
                    if (
                        current is not None
                        and current.identity is not None
                        and current.identity.identity_id != snap.identity.identity_id
                    ):
                        raise StorageError(
                            "Backup belongs to a different Bunny identity than this "
                            "profile; refusing to replace it. Use a fresh --data-dir."
                        )
                merged_watermark = self._merge_live_watermark(
                    snap.identity.identity_id,
                    current.schema_version if current is not None else None,
                )
                safety_copy = self._preserve_current_database(damaged=current_damaged)
                staging = (
                    self.data_dir / f".{DB_FILENAME}.restore-{uuid.uuid4().hex[:8]}.tmp"
                )
                src_conn: sqlite3.Connection | None = None
                dst_conn: sqlite3.Connection | None = None
                stg_conn: sqlite3.Connection | None = None
                reconciled_attempts = 0
                dispatch_blocked = False
                try:
                    src_conn = _open_ro(backup_path, self.busy_timeout_ms)
                    dst_conn = sqlite3.connect(str(staging), isolation_level=None)
                    src_conn.backup(dst_conn)
                    dst_conn.close()
                    dst_conn = None
                    src_conn.close()
                    src_conn = None
                    restrict_path_permissions(staging, is_dir=False)
                    inspect_file(staging, self.busy_timeout_ms)

                    if merged_watermark is not None:
                        if snap.schema_version >= 4:
                            stg_conn = _open_rw(staging, self.busy_timeout_ms)
                            _begin_immediate(stg_conn, self.busy_timeout_ms)
                            stale, reconciled_attempts, final_wm = (
                                reconcile_db_with_watermark(
                                    stg_conn,
                                    snap.identity.identity_id,
                                    merged_watermark,
                                    block_reason="stale_backup_restored_reconcile_required",
                                    now_iso=_utc_now_iso(),
                                )
                            )
                            if stale:
                                stg_conn.execute("COMMIT")
                                dispatch_blocked = True
                                with acquire_exclusive_file_lock(
                                    self.data_dir / LEDGER_LOCK_FILENAME,
                                    self.busy_timeout_ms,
                                ):
                                    write_watermark_atomic(self.data_dir, final_wm)
                            else:
                                stg_conn.execute("ROLLBACK")
                                dispatch_blocked = final_wm.dispatch_blocked
                            stg_conn.close()
                            stg_conn = None
                            inspect_file(staging, self.busy_timeout_ms)
                        elif (
                            merged_watermark.highest_attempt_seq > 0
                            or merged_watermark.attempts
                            or merged_watermark.dispatch_blocked
                        ):
                            reconciled_attempts = len(merged_watermark.attempts)
                            dispatch_blocked = True
                            now_iso = _utc_now_iso()
                            blocked_wm = LedgerWatermark(
                                identity_id=merged_watermark.identity_id,
                                highest_attempt_seq=merged_watermark.highest_attempt_seq,
                                per_request_micro_usd=merged_watermark.per_request_micro_usd,
                                cumulative_micro_usd=merged_watermark.cumulative_micro_usd,
                                dispatch_blocked=True,
                                block_reason="stale_backup_restored_reconcile_required",
                                block_attempt_id=merged_watermark.block_attempt_id,
                                attempts=merged_watermark.attempts,
                                receipts=merged_watermark.receipts,
                                updated_at=now_iso,
                            )
                            with acquire_exclusive_file_lock(
                                self.data_dir / LEDGER_LOCK_FILENAME,
                                self.busy_timeout_ms,
                            ):
                                write_watermark_atomic(self.data_dir, blocked_wm)

                    journal = _journal_path(self.db_path)
                    if journal.exists():
                        journal.unlink()
                    os.replace(staging, self.db_path)
                    restrict_path_permissions(self.db_path, is_dir=False)
                except StorageError:
                    raise
                except (OSError, sqlite3.Error) as exc:
                    raise StorageError(
                        f"Restore failed; the original profile was preserved at "
                        f"'{safety_copy}': {exc}"
                    ) from exc
                finally:
                    for conn in (stg_conn, dst_conn, src_conn):
                        if conn is not None:
                            try:
                                if conn.in_transaction:
                                    conn.execute("ROLLBACK")
                            except sqlite3.Error:
                                pass
                            conn.close()
                    if staging.exists():
                        staging.unlink()
                return RestoreResult(
                    restored_from=backup_path,
                    safety_copy=safety_copy,
                    identity=snap.identity,
                    schema_version=snap.schema_version,
                    ledger_reconciled_attempts=reconciled_attempts,
                    dispatch_blocked=dispatch_blocked,
                )

    def _preserve_current_database(self, *, damaged: bool) -> Path | None:
        if not self.db_path.exists():
            return None
        backups_dir = ensure_data_dir(self.data_dir / "backups")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = backups_dir / f"bunny-pre-recover-{stamp}-{uuid.uuid4().hex[:8]}"
        if damaged:
            # Unreadable or mid-write database: keep the raw bytes untouched.
            raw = base.with_suffix(".unverified")
            shutil.copy2(self.db_path, raw)
            restrict_path_permissions(raw, is_dir=False)
            journal = _journal_path(self.db_path)
            if journal.exists():
                raw_journal = base.with_suffix(".unverified-journal")
                shutil.copy2(journal, raw_journal)
                restrict_path_permissions(raw_journal, is_dir=False)
            return raw
        target = base.with_suffix(".sqlite3")
        src_conn = _open_ro(self.db_path, self.busy_timeout_ms)
        dst_conn = sqlite3.connect(str(target), isolation_level=None)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        restrict_path_permissions(target, is_dir=False)
        return target

    def _create_verified_backup(self, target_version: int) -> Path:
        return self.create_backup(f"pre-v{target_version}")

    def create_backup(self, label: str) -> Path:
        """Create an integrity-verified local backup named ``bunny-<label>-...``.

        Backups are unencrypted copies of the whole database, including
        transcripts and any facts that existed at that moment.
        """
        backups_dir = ensure_data_dir(self.data_dir / "backups")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = (
            backups_dir / f"bunny-{label}-{timestamp}-{uuid.uuid4().hex[:8]}.sqlite3"
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
        self,
        *,
        seed_version: str,
        style_version: str,
        imported: IdentityRecord | None = None,
        populate: Callable[[sqlite3.Connection], None] | None = None,
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

            if imported is None:
                identity_record = IdentityRecord(
                    identity_id=str(uuid.uuid4()),
                    seed_version=seed_version,
                    style_version=style_version,
                    created_at=now,
                    awakened_at=now,
                    onboarding_state="greeted",
                    updated_at=now,
                )
            else:
                identity_record = imported
            staging_conn.execute(
                """
                INSERT INTO identity (
                    singleton, identity_id, seed_version, style_version,
                    created_at, awakened_at, onboarding_state, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity_record.identity_id,
                    identity_record.seed_version,
                    identity_record.style_version,
                    identity_record.created_at,
                    identity_record.awakened_at,
                    identity_record.onboarding_state,
                    identity_record.updated_at,
                ),
            )
            if populate is not None:
                populate(staging_conn)
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
            return identity_record, True
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

    def initialize_imported(
        self,
        identity: IdentityRecord,
        populate: Callable[[sqlite3.Connection], None],
    ) -> InitResult:
        """Atomically create a brand-new profile that carries an imported identity.

        Refuses to touch a profile that already has a database file.
        """
        with _acquire_init_lock(self.data_dir, self.busy_timeout_ms):
            if self.db_path.exists():
                raise StorageError(
                    f"Database '{self.db_path}' already exists; import was not applied."
                )
            self._cleanup_stale_init_files()
            created, _ = self._initialize_fresh(
                seed_version=identity.seed_version,
                style_version=identity.style_version,
                imported=identity,
                populate=populate,
            )
            self._finalize_session_open()
            return InitResult(
                identity=created,
                is_first_launch=False,
                schema_version=CURRENT_SCHEMA_VERSION,
                backup_path=None,
            )

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
                self._finalize_session_open()
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
            self._finalize_session_open()
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
        self,
        identity_id: str,
        *,
        limit: int,
        retention_days: int,
        not_before: str | None = None,
    ) -> tuple[TurnRecord, ...]:
        """Load recent opt-in history turns within the retention window.

        ``not_before`` excludes turns created at or before a memory change so
        stale transcripts cannot automatically repopulate context.
        """
        conn = self._require_conn()
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        try:
            if not_before is not None:
                rows = conn.execute(
                    """
                    SELECT t.turn_id, t.session_id, t.turn_index, t.user_text,
                           t.assistant_text, t.provider_mode, t.created_at
                    FROM turns t
                    JOIN sessions s ON s.session_id = t.session_id
                    WHERE s.identity_id = ? AND s.history_enabled = 1
                      AND t.created_at >= ? AND t.created_at > ?
                    ORDER BY t.created_at DESC, t.turn_index DESC
                    LIMIT ?
                    """,
                    (identity_id, cutoff, not_before, limit),
                ).fetchall()
            else:
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

    def count_turns(self, identity_id: str, *, not_before: str | None = None) -> int:
        """Count stored turns, optionally only those eligible for context."""
        if not_before is not None:
            rows = self.query(
                """
                SELECT COUNT(*) FROM turns t
                JOIN sessions s ON s.session_id = t.session_id
                WHERE s.identity_id = ? AND t.created_at > ?
                """,
                (identity_id, not_before),
                context="count turns",
            )
        else:
            rows = self.query(
                """
                SELECT COUNT(*) FROM turns t
                JOIN sessions s ON s.session_id = t.session_id
                WHERE s.identity_id = ?
                """,
                (identity_id,),
                context="count turns",
            )
        return int(str(rows[0][0]))

    def clear_history(self, identity_id: str) -> tuple[int, int]:
        """Delete every stored transcript turn and session; durable memory is untouched."""
        with self.transaction("clear history") as conn:
            turns = conn.execute(
                """
                DELETE FROM turns WHERE session_id IN (
                    SELECT session_id FROM sessions WHERE identity_id = ?
                )
                """,
                (identity_id,),
            ).rowcount
            sessions = conn.execute(
                "DELETE FROM sessions WHERE identity_id = ?", (identity_id,)
            ).rowcount
        return max(int(turns), 0), max(int(sessions), 0)

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
