import errno
import hashlib
import os
import sqlite3
import subprocess
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bunny_ai.app import AwakeningSession
from bunny_ai.config import (
    AppConfig,
    HistorySettings,
    OnboardingSettings,
    StorageSettings,
)
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
from bunny_ai.migrations import CURRENT_SCHEMA_VERSION, m001_identity
from bunny_ai.storage import (
    DB_FILENAME,
    INIT_STAGING_PREFIX,
    SESSION_LOCK_FILENAME,
    LocalStorage,
    _try_lock_fd,
    _unlock_fd,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_v1_db(db_path: Path, *, identity_id: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        m001_identity.apply(conn)
        conn.execute(
            """
            INSERT INTO schema_migrations (version, applied_at, description)
            VALUES (1, '2026-11-24T00:00:00.000000Z', 'v1')
            """
        )
        conn.execute(
            """
            INSERT INTO identity (
                singleton, identity_id, seed_version, style_version,
                created_at, awakened_at, onboarding_state, updated_at
            )
            VALUES (
                1, ?, 'seed-1', 'style-1',
                '2026-11-24T00:00:00.000000Z',
                '2026-11-24T00:00:00.000000Z',
                'skipped',
                '2026-11-24T00:00:00.000000Z'
            )
            """,
            (identity_id,),
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def test_fresh_init_and_restart_with_spaces_in_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "bunny profile with spaces"
    storage = LocalStorage(data_dir)
    first = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()

    assert first.is_first_launch is True
    assert first.schema_version == CURRENT_SCHEMA_VERSION
    assert first.identity.onboarding_state == "greeted"

    if os.name == "posix":
        assert (data_dir.stat().st_mode & 0o777) == 0o700
        assert (storage.db_path.stat().st_mode & 0o777) == 0o600

    reloaded = LocalStorage(data_dir)
    second = reloaded.initialize_or_load(seed_version="seed-1", style_version="style-1")
    reloaded.close()

    assert second.is_first_launch is False
    assert second.identity.identity_id == first.identity.identity_id
    assert second.identity.created_at == first.identity.created_at


def test_inspect_missing_db_does_not_create_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "missing-dir"
    storage = LocalStorage(data_dir)
    snapshot = storage.inspect()
    assert snapshot.exists is False
    assert snapshot.identity is None
    assert not data_dir.exists()


def test_v1_schema_migrates_with_verified_backup(tmp_path: Path) -> None:
    data_dir = tmp_path / "v1-data"
    data_dir.mkdir()
    db_path = data_dir / DB_FILENAME
    expected_id = "11111111-2222-4333-8444-555555555555"
    _create_v1_db(db_path, identity_id=expected_id)

    storage = LocalStorage(data_dir)
    result = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()

    assert result.is_first_launch is False
    assert result.schema_version == CURRENT_SCHEMA_VERSION
    assert result.identity.identity_id == expected_id
    assert result.identity.onboarding_state == "skipped"
    assert result.backup_path is not None
    assert result.backup_path.is_file()

    snapshot = LocalStorage(data_dir).inspect()
    assert snapshot.schema_version == CURRENT_SCHEMA_VERSION
    assert snapshot.identity is not None
    assert snapshot.identity.identity_id == expected_id


def test_failed_migration_rolls_back_and_preserves_v1_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "v1-fail"
    data_dir.mkdir()
    db_path = data_dir / DB_FILENAME
    expected_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    _create_v1_db(db_path, identity_id=expected_id)
    original_digest = _sha256(db_path)

    from bunny_ai.migrations import MIGRATIONS, Migration

    def _boom(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("boom")

    broken_migrations = tuple(
        migration
        if migration.version == 1
        else Migration(
            version=migration.version,
            description=migration.description,
            apply=_boom,
        )
        for migration in MIGRATIONS
    )
    monkeypatch.setattr("bunny_ai.storage.MIGRATIONS", broken_migrations)

    storage = LocalStorage(data_dir)
    with pytest.raises(MigrationError, match="boom"):
        storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()

    assert _sha256(db_path) == original_digest
    assert list((data_dir / "backups").glob("bunny-pre-v4-*.sqlite3")) != []


def test_corrupt_databases_preserve_bytes(tmp_path: Path) -> None:
    for name, payload in (
        ("empty", b""),
        ("garbage", b"not a sqlite database at all"),
        ("truncated_header", b"SQLite format 3"),
    ):
        data_dir = tmp_path / f"corrupt-{name}"
        data_dir.mkdir()
        db_path = data_dir / DB_FILENAME
        db_path.write_bytes(payload)
        digest = _sha256(db_path)

        storage = LocalStorage(data_dir)
        with pytest.raises(CorruptDatabaseError):
            storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
        assert _sha256(db_path) == digest

    # Valid header + corrupted B-tree bytes
    corrupt_page_dir = tmp_path / "corrupt-page"
    storage = LocalStorage(corrupt_page_dir)
    storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()
    raw = bytearray(storage.db_path.read_bytes())
    for idx in range(100, min(len(raw), 512)):
        raw[idx] ^= 0xFF
    storage.db_path.write_bytes(bytes(raw))
    digest = _sha256(storage.db_path)

    with pytest.raises(CorruptDatabaseError):
        LocalStorage(corrupt_page_dir).initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
    assert _sha256(storage.db_path) == digest


def test_newer_schema_rejected_without_modification(tmp_path: Path) -> None:
    data_dir = tmp_path / "newer"
    storage = LocalStorage(data_dir)
    storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()

    conn = sqlite3.connect(str(storage.db_path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    digest = _sha256(storage.db_path)

    with pytest.raises(NewerSchemaError):
        LocalStorage(data_dir).initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
    assert _sha256(storage.db_path) == digest


def test_unversioned_foreign_schema_rejected_without_modification(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "foreign"
    data_dir.mkdir()
    db_path = data_dir / DB_FILENAME
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes (body) VALUES ('user data')")
    conn.commit()
    conn.close()
    digest = _sha256(db_path)

    with pytest.raises(IncompatibleSchemaError):
        LocalStorage(data_dir).initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
    assert _sha256(db_path) == digest


def test_missing_identity_row_rejected_without_recreating(tmp_path: Path) -> None:
    data_dir = tmp_path / "missing-identity"
    storage = LocalStorage(data_dir)
    storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()

    conn = sqlite3.connect(str(storage.db_path))
    conn.execute("DELETE FROM identity")
    conn.commit()
    conn.close()
    digest = _sha256(storage.db_path)

    with pytest.raises(MissingIdentityError):
        LocalStorage(data_dir).initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
    assert _sha256(storage.db_path) == digest


def test_missing_required_table_rejected_without_modification(tmp_path: Path) -> None:
    data_dir = tmp_path / "missing-table"
    storage = LocalStorage(data_dir)
    storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()

    conn = sqlite3.connect(str(storage.db_path))
    conn.execute("DROP TABLE turns")
    conn.commit()
    conn.close()
    digest = _sha256(storage.db_path)

    with pytest.raises(CorruptDatabaseError, match="turns"):
        LocalStorage(data_dir).initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
    assert _sha256(storage.db_path) == digest


def test_session_closes_cleanly_and_preserves_identity(tmp_path: Path) -> None:
    data_dir = tmp_path / "session-close"
    cfg = AppConfig(
        data_dir=data_dir,
        config_path=None,
        storage=StorageSettings(),
        history=HistorySettings(enabled=True),
        onboarding=OnboardingSettings(),
    )
    session = AwakeningSession(cfg)
    init = session.open()
    session.handle_turn("hello", from_intro=True)
    session.close()
    session.close()

    snapshot = LocalStorage(data_dir).inspect()
    assert snapshot.identity is not None
    assert snapshot.identity.identity_id == init.identity.identity_id
    assert snapshot.session_count == 1
    assert snapshot.turn_count == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory mode semantics")
def test_storage_preserves_existing_dir_mode_and_creates_new_dir_privately_posix(
    tmp_path: Path,
) -> None:
    new_dir = tmp_path / "new-private-data"
    s1 = LocalStorage(new_dir)
    s1.initialize_or_load(seed_version="seed-1", style_version="style-1")
    s1.close()
    assert (new_dir.stat().st_mode & 0o777) == 0o700
    assert (s1.db_path.stat().st_mode & 0o777) == 0o600

    existing_dir = tmp_path / "existing-shared-data"
    existing_dir.mkdir(mode=0o755)
    os.chmod(existing_dir, 0o755)
    s2 = LocalStorage(existing_dir)
    s2.initialize_or_load(seed_version="seed-1", style_version="style-1")
    s2.close()
    assert (existing_dir.stat().st_mode & 0o777) == 0o755
    assert (s2.db_path.stat().st_mode & 0o777) == 0o600


def test_retention_prunes_expired_turns_on_writable_startup_including_no_history(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "retention-data"
    data_dir.mkdir()
    db_path = data_dir / DB_FILENAME
    expected_id = "22222222-3333-4444-8555-666666666666"
    _create_v1_db(db_path, identity_id=expected_id)

    # Upgrade v1 -> v2 so a real preflight backup exists in backups/.
    upgraded = LocalStorage(data_dir)
    init = upgraded.initialize_or_load(seed_version="seed-1", style_version="style-1")
    upgraded.close()
    assert init.backup_path is not None
    backup_digest = _sha256(init.backup_path)

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recent_ts = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-expired', ?, ?, ?, 1)",
            (expected_id, old_ts, old_ts),
        )
        conn.execute(
            """
            INSERT INTO turns VALUES (
                'turn-expired-only', 'sess-expired', 1,
                'old question', 'old answer', 'offline-simulated', ?
            )
            """,
            (old_ts,),
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-mixed', ?, ?, NULL, 1)",
            (expected_id, old_ts),
        )
        conn.execute(
            """
            INSERT INTO turns VALUES (
                'turn-mixed-expired', 'sess-mixed', 1,
                'stale turn', 'stale reply', 'offline-simulated', ?
            )
            """,
            (old_ts,),
        )
        conn.execute(
            """
            INSERT INTO turns VALUES (
                'turn-mixed-recent', 'sess-mixed', 2,
                'fresh turn', 'fresh reply', 'offline-simulated', ?
            )
            """,
            (recent_ts,),
        )
        conn.commit()
    finally:
        conn.close()

    pre_inspect_digest = _sha256(db_path)
    snapshot = LocalStorage(data_dir).inspect()
    assert snapshot.turn_count == 3
    assert _sha256(db_path) == pre_inspect_digest

    # Open a writable no-history session (history_override=False): must prune expired rows.
    cfg = AppConfig(
        data_dir=data_dir,
        config_path=None,
        storage=StorageSettings(),
        history=HistorySettings(enabled=True, retention_days=30),
        onboarding=OnboardingSettings(),
    )
    session = AwakeningSession(cfg, history_override=False)
    session.open()
    assert session.pruned_turn_count == 2
    session.handle_turn("ephemeral turn")
    session.close()

    # Reopen directly and verify persisted rows, identity, backup, and integrity.
    verify_conn = sqlite3.connect(str(db_path))
    try:
        assert verify_conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert verify_conn.execute("SELECT identity_id FROM identity").fetchone() == (
            expected_id,
        )
        remaining_sessions = verify_conn.execute(
            "SELECT session_id FROM sessions ORDER BY session_id"
        ).fetchall()
        assert remaining_sessions == [("sess-mixed",)]
        remaining_turns = verify_conn.execute(
            "SELECT turn_id, user_text FROM turns ORDER BY turn_id"
        ).fetchall()
        assert remaining_turns == [("turn-mixed-recent", "fresh turn")]
    finally:
        verify_conn.close()

    assert _sha256(init.backup_path) == backup_digest


def test_prune_expired_history_preserves_open_session_and_cleans_ended_empty_session(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "open-session-prune"
    storage_a = LocalStorage(data_dir)
    storage_b = LocalStorage(data_dir)
    try:
        init = storage_a.initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
        identity_id = init.identity.identity_id

        closed_session_id = storage_a.start_history_session(identity_id)
        storage_a.record_turn(
            session_id=closed_session_id,
            turn_index=1,
            user_text="closed old turn",
            assistant_text="closed old reply",
            provider_mode="offline-simulated",
        )
        storage_a.end_history_session(closed_session_id)

        open_session_id = storage_a.start_history_session(identity_id)
        storage_a.record_turn(
            session_id=open_session_id,
            turn_index=1,
            user_text="open old turn",
            assistant_text="open old reply",
            provider_mode="offline-simulated",
        )

        # Second live connection prunes past the 30-day cutoff using the injected clock.
        loaded_b = storage_b.initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
        assert loaded_b.identity.identity_id == identity_id
        deleted = storage_b.prune_expired_history(
            identity_id,
            retention_days=30,
            now=datetime.now(UTC) + timedelta(days=31),
        )
        assert deleted == 2

        # First live connection can still save its next turn into the open session.
        storage_a.record_turn(
            session_id=open_session_id,
            turn_index=2,
            user_text="next turn in open session",
            assistant_text="next reply in open session",
            provider_mode="offline-simulated",
        )
    finally:
        storage_b.close()
        storage_a.close()

    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        sessions = conn.execute(
            "SELECT session_id, ended_at FROM sessions ORDER BY session_id"
        ).fetchall()
        assert sessions == [(open_session_id, None)]
        turns = conn.execute(
            "SELECT session_id, turn_index, user_text FROM turns ORDER BY turn_index"
        ).fetchall()
        assert turns == [(open_session_id, 2, "next turn in open session")]
    finally:
        conn.close()


def _create_hot_journal_crash(db_path: Path, *, statements: list[str]) -> Path:
    import json

    script = (
        "import json, os, sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "conn.execute('PRAGMA journal_mode = DELETE')\n"
        "conn.execute('PRAGMA synchronous = FULL')\n"
        "conn.execute('PRAGMA cache_size = 1')\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "for stmt in json.loads(sys.stdin.read()):\n"
        "    conn.execute(stmt)\n"
        "os._exit(1)\n"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(db_path)],
        input=json.dumps(statements),
        text=True,
        encoding="utf-8",
        check=False,
    )
    journal_path = db_path.parent / f"{db_path.name}-journal"
    assert journal_path.is_file() and journal_path.stat().st_size > 0
    return journal_path


def test_interrupted_first_init_is_retryable_and_preserves_unrelated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "retry-init"

    from bunny_ai.migrations import MIGRATIONS, Migration

    def _interrupt(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("interrupted mid-init")

    broken = tuple(
        migration
        if migration.version == 1
        else Migration(
            version=migration.version,
            description=migration.description,
            apply=_interrupt,
        )
        for migration in MIGRATIONS
    )
    monkeypatch.setattr("bunny_ai.storage.MIGRATIONS", broken)

    storage = LocalStorage(data_dir)
    with pytest.raises(StorageError, match="interrupted mid-init"):
        storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()
    assert not storage.db_path.exists()
    assert list(data_dir.glob(f"{INIT_STAGING_PREFIX}*")) == []

    monkeypatch.undo()

    # Simulate stale staging files left behind by a hard-killed first launch,
    # alongside similarly named lookalike files that must be preserved.
    stale_db = data_dir / f"{INIT_STAGING_PREFIX}deadbeef.tmp"
    stale_journal = data_dir / f"{INIT_STAGING_PREFIX}deadbeef.tmp-journal"
    lookalike_backup = data_dir / f"{INIT_STAGING_PREFIX}deadbeef.tmp.backup"
    lookalike_invalid = data_dir / f"{INIT_STAGING_PREFIX}nothex01.tmp"
    stale_db.write_bytes(b"partial")
    stale_journal.write_bytes(b"partial-journal")
    lookalike_backup.write_bytes(b"keep-backup-bytes")
    lookalike_invalid.write_bytes(b"keep-invalid-bytes")

    assert LocalStorage(data_dir).inspect().exists is False

    retried = LocalStorage(data_dir)
    init = retried.initialize_or_load(seed_version="seed-1", style_version="style-1")
    retried.close()
    assert init.is_first_launch is True
    assert not stale_db.exists()
    assert not stale_journal.exists()
    assert lookalike_backup.read_bytes() == b"keep-backup-bytes"
    assert lookalike_invalid.read_bytes() == b"keep-invalid-bytes"

    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        assert conn.execute("SELECT COUNT(*) FROM identity").fetchone() == (1,)
    finally:
        conn.close()

    # Pre-existing empty or damaged files remain untouched.
    for name, payload in (("empty", b""), ("damaged", b"not sqlite")):
        unrelated_dir = tmp_path / f"unrelated-{name}"
        unrelated_dir.mkdir()
        unrelated_db = unrelated_dir / DB_FILENAME
        unrelated_db.write_bytes(payload)
        digest = _sha256(unrelated_db)
        with pytest.raises(CorruptDatabaseError):
            LocalStorage(unrelated_dir).initialize_or_load(
                seed_version="seed-1", style_version="style-1"
            )
        assert _sha256(unrelated_db) == digest


def test_hot_journal_recovery_in_storage_and_refusal_preservation(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "hot-journal"
    storage = LocalStorage(data_dir)
    init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    identity_id = init.identity.identity_id
    sid = storage.start_history_session(identity_id)
    storage.record_turn(
        session_id=sid,
        turn_index=1,
        user_text="committed turn",
        assistant_text="committed reply",
        provider_mode="offline-simulated",
    )
    storage.end_history_session(sid)
    storage.close()

    crash_statements = [
        (
            f"INSERT INTO sessions VALUES ('crash-sess', '{identity_id}', "
            "'2026-11-25T00:00:00Z', NULL, 1)"
        ),
        *[
            (
                f"INSERT INTO turns VALUES ('crash-turn-{idx}', 'crash-sess', {idx}, "
                f"'{('x' * 8192)}', 'crash reply', 'offline-simulated', '2026-11-25T00:00:00Z')"
            )
            for idx in range(1, 10)
        ],
    ]
    journal_path = _create_hot_journal_crash(
        storage.db_path, statements=crash_statements
    )
    db_digest = _sha256(storage.db_path)
    journal_digest = _sha256(journal_path)

    with pytest.raises(RecoveryRequiredError, match="bunny chat"):
        LocalStorage(data_dir).inspect()
    assert _sha256(storage.db_path) == db_digest
    assert _sha256(journal_path) == journal_digest

    recovered = LocalStorage(data_dir)
    resumed = recovered.initialize_or_load(
        seed_version="seed-1", style_version="style-1"
    )
    turns = recovered.load_recent_turns(identity_id, limit=10, retention_days=30)
    recovered.close()
    assert resumed.identity.identity_id == identity_id
    assert [t.user_text for t in turns] == ["committed turn"]
    assert not journal_path.exists()

    # Newer-schema DB with a hot journal must still refuse and preserve both files.
    newer_dir = tmp_path / "newer-hot"
    newer_storage = LocalStorage(newer_dir)
    newer_init = newer_storage.initialize_or_load(
        seed_version="seed-1", style_version="style-1"
    )
    newer_storage.close()
    conn = sqlite3.connect(str(newer_storage.db_path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    newer_crash_statements = [
        (
            f"INSERT INTO sessions VALUES ('crash-sess', '{newer_init.identity.identity_id}', "
            "'2026-11-25T00:00:00Z', NULL, 1)"
        ),
        *[
            (
                f"INSERT INTO turns VALUES ('crash-turn-{idx}', 'crash-sess', {idx}, "
                f"'{('y' * 8192)}', 'reply', 'offline-simulated', '2026-11-25T00:00:00Z')"
            )
            for idx in range(1, 10)
        ],
    ]
    newer_journal = _create_hot_journal_crash(
        newer_storage.db_path, statements=newer_crash_statements
    )
    newer_db_digest = _sha256(newer_storage.db_path)
    newer_journal_digest = _sha256(newer_journal)

    with pytest.raises(NewerSchemaError):
        LocalStorage(newer_dir).initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
    assert _sha256(newer_storage.db_path) == newer_db_digest
    assert _sha256(newer_journal) == newer_journal_digest


def test_lock_contention_raises_database_locked_error(tmp_path: Path) -> None:
    data_dir = tmp_path / "locked"
    storage = LocalStorage(data_dir)
    init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    storage.close()

    holder = sqlite3.connect(str(storage.db_path), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(DatabaseLockedError):
            LocalStorage(data_dir, busy_timeout_ms=150).inspect()
        with pytest.raises(DatabaseLockedError):
            LocalStorage(data_dir, busy_timeout_ms=150).initialize_or_load(
                seed_version="seed-1", style_version="style-1"
            )
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    after = LocalStorage(data_dir)
    loaded = after.initialize_or_load(seed_version="seed-1", style_version="style-1")
    after.close()
    assert loaded.identity.identity_id == init.identity.identity_id


def test_session_locks_allow_concurrent_sessions_and_block_restore_until_all_close(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "session-lock"
    storage_a = LocalStorage(data_dir)
    storage_b = LocalStorage(data_dir)
    backup: Path | None = None
    try:
        init_a = storage_a.initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
        backup = storage_a.create_backup("manual")
        init_b = storage_b.initialize_or_load(
            seed_version="seed-1", style_version="style-1"
        )
        assert init_b.identity.identity_id == init_a.identity.identity_id
        assert (data_dir / SESSION_LOCK_FILENAME).is_file()
        assert storage_a._session_lock_fd is not None
        assert storage_b._session_lock_fd is not None

        # Calling _acquire_session_shared again on an open instance is a no-op.
        held_fd = storage_a._session_lock_fd
        storage_a._acquire_session_shared()
        assert storage_a._session_lock_fd == held_fd

        with pytest.raises(DatabaseLockedError, match="Close active `bunny chat`"):
            LocalStorage(data_dir).restore_from_backup(backup)

        storage_a.close()
        storage_a.close()  # idempotent
        assert storage_a._session_lock_fd is None

        # Still blocked while storage_b remains open.
        with pytest.raises(DatabaseLockedError, match="Close active `bunny chat`"):
            LocalStorage(data_dir).restore_from_backup(backup)

        storage_b.close()
        assert storage_b._session_lock_fd is None
    finally:
        storage_b.close()
        storage_a.close()

    assert backup is not None
    restored = LocalStorage(data_dir).restore_from_backup(backup)
    assert restored.identity.identity_id == init_a.identity.identity_id


def test_posix_session_lock_helpers_via_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_file = tmp_path / "posix.lock"
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    calls: list[tuple[int, int]] = []
    raise_mode: list[OSError | None] = [None]

    def _fake_flock(target_fd: int, op: int) -> None:
        calls.append((target_fd, op))
        if raise_mode[0] is not None:
            raise raise_mode[0]

    fake_fcntl = types.SimpleNamespace(
        LOCK_SH=1,
        LOCK_EX=2,
        LOCK_NB=4,
        LOCK_UN=8,
        flock=_fake_flock,
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr("bunny_ai.storage.sys.platform", "linux")

    closed = False
    try:
        assert _try_lock_fd(fd, exclusive=False) is True
        assert _try_lock_fd(fd, exclusive=True) is True

        raise_mode[0] = BlockingIOError(
            errno.EAGAIN, "Resource temporarily unavailable"
        )
        assert _try_lock_fd(fd, exclusive=True) is False

        raise_mode[0] = OSError(errno.EINVAL, "Bad file descriptor")
        with pytest.raises(OSError, match="Bad file descriptor"):
            _try_lock_fd(fd, exclusive=False)

        raise_mode[0] = None
        _unlock_fd(fd)
        closed = True
    finally:
        if not closed:
            os.close(fd)

    assert calls == [
        (fd, 1 | 4),
        (fd, 2 | 4),
        (fd, 2 | 4),
        (fd, 1 | 4),
        (fd, 8),
    ]
    with pytest.raises(OSError):
        os.fstat(fd)
