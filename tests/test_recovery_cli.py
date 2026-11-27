"""Installed-CLI tests for profile session locking and recovery classification."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from chat_driver import ChatDriver
from test_cli_integration import (
    _cli_env,
    _extract_identity,
    _run_cli,
    _sha256,
)

from bunny_ai.storage import (
    DB_FILENAME,
    LOCK_FILENAME,
    SESSION_LOCK_FILENAME,
    LocalStorage,
)

NAME = "Zorblax"
PREF = "Prefers answers in haiku form"


def _chat(tmp_path: Path, data_dir: Path, script: str, *extra: str) -> str:
    result = _run_cli(
        ["chat", "--data-dir", str(data_dir), *extra],
        env=_cli_env(tmp_path),
        cwd=tmp_path,
        stdin_text=script,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _seed_profile(tmp_path: Path, data_dir: Path, *, history: bool = False) -> str:
    """Create a disposable v3 profile with one saved fact and return its identity."""
    extra = ("--history",) if history else ()
    out = _chat(
        tmp_path,
        data_dir,
        f"/skip\n/remember name {NAME}\ny\n/exit\n",
        *extra,
    )
    return _extract_identity(out)


def _snapshot(data_dir: Path) -> tuple[str, list[str]]:
    ignored = {LOCK_FILENAME, SESSION_LOCK_FILENAME}
    return _sha256(data_dir / DB_FILENAME), sorted(
        str(p.relative_to(data_dir))
        for p in data_dir.rglob("*")
        if p.name not in ignored
    )


def _create_backup(data_dir: Path, label: str = "manual") -> Path:
    storage = LocalStorage(data_dir)
    try:
        storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
        return storage.create_backup(label)
    finally:
        storage.close()


def _create_hot_journal_crash(db_path: Path, identity_id: str) -> Path:
    statements = [
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


def test_recover_refuses_idle_open_chat_and_preserves_concurrent_chat_continuity(
    tmp_path: Path,
) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "profile"
    identity = _seed_profile(tmp_path, data_dir, history=True)
    backup = _create_backup(data_dir, "seed")

    with ChatDriver(["--data-dir", str(data_dir), "--history"], env, tmp_path) as chat1:
        assert "Welcome back" in chat1.opening
        turn1 = chat1.send("turn one while idle")
        assert "[Offline simulated | seed-1]" in turn1

        # Normal concurrent chat still opens and works while chat1 is idle.
        with ChatDriver(
            ["--data-dir", str(data_dir), "--history"], env, tmp_path
        ) as chat2:
            assert "Welcome back" in chat2.opening
            concurrent_out = chat2.send("concurrent chat turn")
            assert "[Offline simulated | seed-1]" in concurrent_out
            assert chat2.finish() == 0

        before = _snapshot(data_dir)
        backups_before = sorted(p.name for p in (data_dir / "backups").iterdir())
        refused = _run_cli(
            [
                "recover",
                "--backup",
                backup.name,
                "--yes",
                "--data-dir",
                str(data_dir),
            ],
            env=env,
            cwd=tmp_path,
        )
        assert refused.returncode == 1
        assert "open in another Bunny session" in refused.stderr
        assert "Traceback" not in refused.stderr
        assert _snapshot(data_dir) == before
        assert (
            sorted(p.name for p in (data_dir / "backups").iterdir()) == backups_before
        )
        assert list((data_dir / "backups").glob("bunny-pre-recover-*")) == []

        turn2 = chat1.send("turn two after refused recover")
        assert "[Offline simulated | seed-1]" in turn2
        assert chat1.finish() == 0

    status = _run_cli(
        ["status", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
    )
    assert status.returncode == 0, status.stderr
    assert f"Identity: {identity}" in status.stdout
    assert "Saved turns: 3" in status.stdout

    with ChatDriver(["--data-dir", str(data_dir), "--history"], env, tmp_path) as fresh:
        assert "3 prior turn(s) loaded" in fresh.opening
        assert fresh.finish() == 0

    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        saved_user_turns = [
            row[0]
            for row in conn.execute(
                "SELECT user_text FROM turns ORDER BY created_at, turn_index"
            ).fetchall()
        ]
        assert saved_user_turns == [
            "turn one while idle",
            "concurrent chat turn",
            "turn two after refused recover",
        ]
    finally:
        conn.close()


def test_recover_refuses_locked_newer_foreign_and_wrong_identity_without_copies(
    tmp_path: Path,
) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "profile"
    _seed_profile(tmp_path, data_dir)
    backup = _create_backup(data_dir, "seed")
    fast_cfg = tmp_path / "fast.toml"
    fast_cfg.write_text("[storage]\nbusy_timeout_ms = 200\n", encoding="utf-8")

    # 1. External SQLite BEGIN EXCLUSIVE lock -> refused cleanly, no safety copy.
    before_locked = _snapshot(data_dir)
    holder = sqlite3.connect(str(data_dir / DB_FILENAME), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        locked_res = _run_cli(
            [
                "recover",
                "--config",
                str(fast_cfg),
                "--backup",
                backup.name,
                "--yes",
                "--data-dir",
                str(data_dir),
            ],
            env=env,
            cwd=tmp_path,
        )
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert locked_res.returncode == 1
    assert "locked" in locked_res.stderr.lower()
    assert "Traceback" not in locked_res.stderr
    assert _snapshot(data_dir) == before_locked
    assert list((data_dir / "backups").glob("bunny-pre-recover-*")) == []

    # 2. Wrong-identity backup -> refused cleanly, no safety copy.
    stranger_dir = tmp_path / "stranger"
    _seed_profile(tmp_path, stranger_dir)
    foreign_backup = tmp_path / "stranger.sqlite3"
    shutil.copyfile(stranger_dir / DB_FILENAME, foreign_backup)
    before_wrong_id = _snapshot(data_dir)
    wrong_id_res = _run_cli(
        [
            "recover",
            "--backup",
            str(foreign_backup),
            "--yes",
            "--data-dir",
            str(data_dir),
        ],
        env=env,
        cwd=tmp_path,
    )
    assert wrong_id_res.returncode == 1
    assert "different Bunny identity" in wrong_id_res.stderr
    assert "Traceback" not in wrong_id_res.stderr
    assert _snapshot(data_dir) == before_wrong_id
    assert list((data_dir / "backups").glob("bunny-pre-recover-*")) == []

    # 3. Newer-schema DB (user_version = 99) -> refused cleanly, no safety copy.
    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()
    before_newer = _snapshot(data_dir)
    newer_res = _run_cli(
        [
            "recover",
            "--backup",
            backup.name,
            "--yes",
            "--data-dir",
            str(data_dir),
        ],
        env=env,
        cwd=tmp_path,
    )
    assert newer_res.returncode == 1
    assert "newer than supported" in newer_res.stderr
    assert "Traceback" not in newer_res.stderr
    assert _snapshot(data_dir) == before_newer
    assert list((data_dir / "backups").glob("bunny-pre-recover-*")) == []

    # 4. Foreign unversioned SQLite DB -> refused cleanly, no safety copy.
    (data_dir / DB_FILENAME).unlink()
    foreign_conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        foreign_conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        foreign_conn.execute("INSERT INTO notes (body) VALUES ('user notes')")
        foreign_conn.commit()
    finally:
        foreign_conn.close()
    before_foreign = _snapshot(data_dir)
    foreign_res = _run_cli(
        [
            "recover",
            "--backup",
            backup.name,
            "--yes",
            "--data-dir",
            str(data_dir),
        ],
        env=env,
        cwd=tmp_path,
    )
    assert foreign_res.returncode == 1
    assert "unversioned tables" in foreign_res.stderr
    assert "Traceback" not in foreign_res.stderr
    assert _snapshot(data_dir) == before_foreign
    assert list((data_dir / "backups").glob("bunny-pre-recover-*")) == []


def test_recover_preserves_healthy_and_damaged_profile_flows(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "profile"
    identity = _seed_profile(tmp_path, data_dir)
    backup = _create_backup(data_dir, "one-fact")

    # Add a second fact so healthy restore visibly rolls back to 1 fact.
    _chat(tmp_path, data_dir, f"/remember preference {PREF}\ny\n/exit\n")
    status_two = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert "Saved facts: 2" in status_two.stdout

    # 1. Healthy same-identity recovery when no chat is open -> .sqlite3 safety copy.
    healthy = _run_cli(
        ["recover", "--backup", backup.name, "--yes", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
    )
    assert healthy.returncode == 0, healthy.stderr
    assert "Previous database preserved at:" in healthy.stdout
    sqlite_copies = list((data_dir / "backups").glob("bunny-pre-recover-*.sqlite3"))
    assert len(sqlite_copies) == 1
    assert list((data_dir / "backups").glob("bunny-pre-recover-*.unverified")) == []
    assert (
        "Saved facts: 1"
        in _run_cli(
            ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
        ).stdout
    )

    # 2. Corrupt bytes -> .unverified safety copy + restored working profile.
    corrupt_bytes = b"not a valid sqlite database"
    (data_dir / DB_FILENAME).write_bytes(corrupt_bytes)
    corrupt_res = _run_cli(
        ["recover", "--backup", backup.name, "--yes", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
    )
    assert corrupt_res.returncode == 0, corrupt_res.stderr
    unverified_copies = list(
        (data_dir / "backups").glob("bunny-pre-recover-*.unverified")
    )
    assert len(unverified_copies) == 1
    assert unverified_copies[0].read_bytes() == corrupt_bytes
    corrupt_status = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert f"Identity: {identity}" in corrupt_status.stdout
    assert "Saved facts: 1" in corrupt_status.stdout

    # 3. Missing identity row -> .unverified safety copy + restored working profile.
    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        conn.execute("DELETE FROM identity")
        conn.commit()
    finally:
        conn.close()
    missing_id_res = _run_cli(
        ["recover", "--backup", backup.name, "--yes", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
    )
    assert missing_id_res.returncode == 0, missing_id_res.stderr
    unverified_after_missing = list(
        (data_dir / "backups").glob("bunny-pre-recover-*.unverified")
    )
    assert len(unverified_after_missing) == 2
    missing_status = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert f"Identity: {identity}" in missing_status.stdout
    assert "Saved facts: 1" in missing_status.stdout

    # 4. Hot rollback journal -> .unverified and .unverified-journal safety copies.
    journal_path = _create_hot_journal_crash(data_dir / DB_FILENAME, identity)
    journal_res = _run_cli(
        ["recover", "--backup", backup.name, "--yes", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
    )
    assert journal_res.returncode == 0, journal_res.stderr
    assert not journal_path.exists()
    assert len(list((data_dir / "backups").glob("bunny-pre-recover-*.unverified"))) == 3
    assert (
        len(list((data_dir / "backups").glob("bunny-pre-recover-*.unverified-journal")))
        == 1
    )
    journal_status = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert f"Identity: {identity}" in journal_status.stdout
    assert "Saved facts: 1" in journal_status.stdout
