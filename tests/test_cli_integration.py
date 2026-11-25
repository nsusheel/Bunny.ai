import hashlib
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bunny_ai.migrations import m001_identity
from bunny_ai.storage import DB_FILENAME

NETWORK_GUARD = """\
import socket


def denied(*args, **kwargs):
    raise RuntimeError("Bunny CLI integration test blocked the network")


socket.socket = denied
socket.getaddrinfo = denied
"""

IDENTITY_RE = re.compile(r"^Identity:\s+([0-9a-f-]+)$", re.MULTILINE)


def _installed_bunny() -> Path:
    bin_dir = Path(sys.executable).parent
    bunny = bin_dir / ("bunny.exe" if os.name == "nt" else "bunny")
    assert bunny.is_file(), f"Installed CLI not found at {bunny}"
    return bunny


def _cli_env(tmp_path: Path) -> dict[str, str]:
    guard_dir = tmp_path / "_net_guard"
    guard_dir.mkdir(exist_ok=True)
    (guard_dir / "sitecustomize.py").write_text(NETWORK_GUARD, encoding="utf-8")

    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}
    }
    profile_root = tmp_path / "_isolated_home"
    profile_root.mkdir(exist_ok=True)
    env.update(
        PYTHONPATH=str(guard_dir),
        PYTHONNOUSERSITE="1",
        PYTHONUTF8="1",
        HOME=str(profile_root),
        USERPROFILE=str(profile_root),
        APPDATA=str(profile_root / "appdata"),
        LOCALAPPDATA=str(profile_root / "localappdata"),
        XDG_CONFIG_HOME=str(profile_root / "xdg-config"),
        XDG_DATA_HOME=str(profile_root / "xdg-data"),
    )
    return env


def _run_cli(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_installed_bunny()), *args],
        cwd=cwd,
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


def _extract_identity(stdout: str) -> str:
    match = IDENTITY_RE.search(stdout)
    assert match is not None, f"Identity line not found in:\n{stdout}"
    return match.group(1)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_first_launch_skip_question_and_restart(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "data"

    # First launch: greet, skip optional question with empty Enter, take a turn, exit cleanly.
    first = _run_cli(
        ["chat", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
        stdin_text="\nHello Bunny\n/exit\n",
    )
    assert first.returncode == 0, first.stderr
    assert first.stderr == ""
    assert "First launch" in first.stdout
    assert "Anything you'd like to explore" in first.stdout
    assert "Skipped intro question." in first.stdout
    assert "[Offline simulated | seed-1]" in first.stdout
    assert "Bunny session closed." in first.stdout
    first_id = _extract_identity(first.stdout)

    # Restart in a new process: same identity, no onboarding replay.
    restart = _run_cli(
        ["chat", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
        stdin_text="How are you?\n/quit\n",
    )
    assert restart.returncode == 0, restart.stderr
    assert restart.stderr == ""
    assert "Returning" in restart.stdout
    assert "Welcome back!" in restart.stdout
    assert "Anything you'd like to explore" not in restart.stdout
    assert _extract_identity(restart.stdout) == first_id

    # Default opt-in history is off: zero turns persisted.
    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        assert conn.execute("SELECT COUNT(*) FROM identity").fetchone() == (1,)
        assert conn.execute("SELECT onboarding_state FROM identity").fetchone() == (
            "skipped",
        )
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone() == (0,)
    finally:
        conn.close()


def test_first_launch_skip_with_slash_skip(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "skip-cmd-data"

    result = _run_cli(
        ["chat", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
        stdin_text="/skip\nSecond line\n/exit\n",
    )
    assert result.returncode == 0, result.stderr
    assert "Skipped intro question." in result.stdout
    assert "11 chars" in result.stdout


def test_opt_in_history_and_no_history_session_override(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "history-data"
    cfg_file = tmp_path / "bunny.toml"
    cfg_file.write_text("[history]\nenabled = true\n", encoding="utf-8")

    # Session 1 (opt-in history via TOML): persists turn 1.
    s1 = _run_cli(
        [
            "chat",
            "--config",
            str(cfg_file),
            "--data-dir",
            str(data_dir),
            "--skip-intro",
            "-m",
            "Remembered locally",
        ],
        env=env,
        cwd=tmp_path,
    )
    assert s1.returncode == 0, s1.stderr
    first_id = _extract_identity(s1.stdout)
    assert "History: on (opt-in local SQLite)" in s1.stdout

    # Session 2 (--no-history override): sees same identity, loads 0 prior turns, writes 0 rows.
    s2 = _run_cli(
        [
            "chat",
            "--config",
            str(cfg_file),
            "--data-dir",
            str(data_dir),
            "--no-history",
            "-m",
            "Private ephemeral turn",
        ],
        env=env,
        cwd=tmp_path,
    )
    assert s2.returncode == 0, s2.stderr
    assert _extract_identity(s2.stdout) == first_id
    assert "History: off (no-history session)" in s2.stdout
    assert "prior local turn" not in s2.stdout

    # Session 3 (opt-in history again): restores only the 1 saved turn from Session 1.
    s3 = _run_cli(
        [
            "chat",
            "--config",
            str(cfg_file),
            "--data-dir",
            str(data_dir),
            "-m",
            "Follow-up",
        ],
        env=env,
        cwd=tmp_path,
    )
    assert s3.returncode == 0, s3.stderr
    assert _extract_identity(s3.stdout) == first_id
    assert "1 prior turn(s) loaded" in s3.stdout
    assert "1 prior local turn(s) in context" in s3.stdout

    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        rows = conn.execute(
            "SELECT user_text FROM turns ORDER BY created_at"
        ).fetchall()
        assert rows == [("Remembered locally",), ("Follow-up",)]
        open_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL"
        ).fetchone()
        assert open_sessions == (0,)
    finally:
        conn.close()


def test_two_simultaneous_first_launches_yield_one_identity(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    bunny = str(_installed_bunny())

    for round_idx in range(3):
        data_dir = tmp_path / f"concurrent-{round_idx}"
        cmd = [
            bunny,
            "chat",
            "--data-dir",
            str(data_dir),
            "--skip-intro",
            "-m",
            f"hello-{round_idx}",
        ]
        proc_a = subprocess.Popen(
            cmd,
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        proc_b = subprocess.Popen(
            cmd,
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            out_a, err_a = proc_a.communicate(timeout=15)
            out_b, err_b = proc_b.communicate(timeout=15)
        finally:
            for proc in (proc_a, proc_b):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

        assert proc_a.returncode == 0, err_a
        assert proc_b.returncode == 0, err_b
        assert err_a == ""
        assert err_b == ""

        id_a = _extract_identity(out_a)
        id_b = _extract_identity(out_b)
        assert id_a == id_b

        launches = sorted(
            [
                "first" if "First launch" in out else "returning"
                for out in (out_a, out_b)
            ]
        )
        assert launches == ["first", "returning"]

        conn = sqlite3.connect(str(data_dir / DB_FILENAME))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert conn.execute("SELECT COUNT(*) FROM identity").fetchone() == (1,)
            assert conn.execute("SELECT identity_id FROM identity").fetchone() == (
                id_a,
            )
        finally:
            conn.close()


def test_clean_shutdown_on_eof_mid_greeting_and_mid_chat(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "eof-data"

    # EOF immediately at intro prompt.
    eof_intro = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--history"],
        env=env,
        cwd=tmp_path,
        stdin_text="",
    )
    assert eof_intro.returncode == 0, eof_intro.stderr
    assert "Bunny session closed." in eof_intro.stdout
    first_id = _extract_identity(eof_intro.stdout)

    # EOF in chat after one turn.
    eof_chat = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--history"],
        env=env,
        cwd=tmp_path,
        stdin_text="One turn before EOF\n",
    )
    assert eof_chat.returncode == 0, eof_chat.stderr
    assert "Bunny session closed." in eof_chat.stdout
    assert _extract_identity(eof_chat.stdout) == first_id

    db_path = data_dir / DB_FILENAME
    assert not (data_dir / f"{DB_FILENAME}-journal").exists()
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL"
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT console signal test")
def test_clean_shutdown_on_sigint_posix(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "sigint-data"
    proc = subprocess.Popen(
        [str(_installed_bunny()), "chat", "--data-dir", str(data_dir), "--history"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.3)
    proc.send_signal(signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, stderr
    assert "Bunny session closed." in stdout


def test_existing_v1_migration_and_failure_schema_preservation(
    tmp_path: Path,
) -> None:
    env = _cli_env(tmp_path)

    # 1. Missing DB: status creates nothing.
    missing_dir = tmp_path / "missing"
    missing_status = _run_cli(
        ["status", "--data-dir", str(missing_dir)], env=env, cwd=tmp_path
    )
    assert missing_status.returncode == 0, missing_status.stderr
    assert "State: not initialized" in missing_status.stdout
    assert not missing_dir.exists()

    # 2. Pre-existing v1 DB: CLI migrates safely with backup and keeps identity.
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir()
    v1_db = v1_dir / DB_FILENAME
    v1_id = "12345678-1234-4123-8123-1234567890ab"
    conn = sqlite3.connect(str(v1_db))
    m001_identity.apply(conn)
    conn.execute(
        "INSERT INTO schema_migrations VALUES (1, '2026-11-24T00:00:00Z', 'v1')"
    )
    conn.execute(
        """
        INSERT INTO identity VALUES (
            1, ?, 'seed-1', 'style-1',
            '2026-11-24T00:00:00Z', '2026-11-24T00:00:00Z',
            'answered', '2026-11-24T00:00:00Z'
        )
        """,
        (v1_id,),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    migrated = _run_cli(
        ["chat", "--data-dir", str(v1_dir), "-m", "After upgrade"],
        env=env,
        cwd=tmp_path,
    )
    assert migrated.returncode == 0, migrated.stderr
    assert _extract_identity(migrated.stdout) == v1_id
    assert "Preflight backup:" in migrated.stdout
    assert list((v1_dir / "backups").glob("bunny-pre-v2-*.sqlite3")) != []

    # 3. Corrupt DB, newer schema, unversioned foreign DB, and missing identity row.
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir()
    corrupt_db = corrupt_dir / DB_FILENAME
    corrupt_db.write_bytes(b"corrupt non-sqlite payload")
    corrupt_digest = _sha256(corrupt_db)

    corrupt_res = _run_cli(
        ["chat", "--data-dir", str(corrupt_dir), "-m", "hi"], env=env, cwd=tmp_path
    )
    assert corrupt_res.returncode == 1
    assert "Storage error:" in corrupt_res.stderr
    assert _sha256(corrupt_db) == corrupt_digest

    newer_dir = tmp_path / "newer"
    init_newer = _run_cli(
        ["chat", "--data-dir", str(newer_dir), "--skip-intro", "-m", "init"],
        env=env,
        cwd=tmp_path,
    )
    assert init_newer.returncode == 0
    newer_db = newer_dir / DB_FILENAME
    conn = sqlite3.connect(str(newer_db))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    newer_digest = _sha256(newer_db)

    newer_res = _run_cli(
        ["chat", "--data-dir", str(newer_dir), "-m", "hi"], env=env, cwd=tmp_path
    )
    assert newer_res.returncode == 1
    assert "newer than supported" in newer_res.stderr
    assert _sha256(newer_db) == newer_digest

    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    foreign_db = foreign_dir / DB_FILENAME
    conn = sqlite3.connect(str(foreign_db))
    conn.execute("CREATE TABLE custom (x TEXT)")
    conn.execute("INSERT INTO custom VALUES ('keep me')")
    conn.commit()
    conn.close()
    foreign_digest = _sha256(foreign_db)

    foreign_res = _run_cli(
        ["status", "--data-dir", str(foreign_dir)], env=env, cwd=tmp_path
    )
    assert foreign_res.returncode == 1
    assert "Storage error:" in foreign_res.stderr
    assert _sha256(foreign_db) == foreign_digest

    missing_id_dir = tmp_path / "missing-id"
    assert (
        _run_cli(
            ["chat", "--data-dir", str(missing_id_dir), "--skip-intro", "-m", "init"],
            env=env,
            cwd=tmp_path,
        ).returncode
        == 0
    )
    missing_id_db = missing_id_dir / DB_FILENAME
    conn = sqlite3.connect(str(missing_id_db))
    conn.execute("DELETE FROM identity")
    conn.commit()
    conn.close()
    missing_id_digest = _sha256(missing_id_db)

    missing_id_res = _run_cli(
        ["chat", "--data-dir", str(missing_id_dir), "-m", "hi"], env=env, cwd=tmp_path
    )
    assert missing_id_res.returncode == 1
    assert "identity record is missing" in missing_id_res.stderr
    assert _sha256(missing_id_db) == missing_id_digest


def test_demo_remains_stateless_via_installed_cli(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    env["BUNNY_DATA_DIR"] = str(tmp_path / "should-never-exist")
    res = _run_cli(["demo", "Hello Bunny"], env=env, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "[Offline demo, simulated] Received 11 characters." in res.stdout
    assert not (tmp_path / "should-never-exist").exists()


def test_retention_pruning_cli_no_history_startup_and_readonly_status(
    tmp_path: Path,
) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "cli-retention"
    data_dir.mkdir()
    db_path = data_dir / DB_FILENAME
    expected_id = "33333333-4444-4555-8666-777777777777"

    # Seed a v1 DB so upgrading produces a real preflight backup in backups/.
    conn = sqlite3.connect(str(db_path))
    m001_identity.apply(conn)
    conn.execute(
        "INSERT INTO schema_migrations VALUES (1, '2026-11-24T00:00:00Z', 'v1')"
    )
    conn.execute(
        """
        INSERT INTO identity VALUES (
            1, ?, 'seed-1', 'style-1',
            '2026-11-24T00:00:00Z', '2026-11-24T00:00:00Z',
            'skipped', '2026-11-24T00:00:00Z'
        )
        """,
        (expected_id,),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    # Run a history-enabled turn (migrates to v2, creates backup, saves recent turn).
    recent = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--history", "-m", "recent turn"],
        env=env,
        cwd=tmp_path,
    )
    assert recent.returncode == 0, recent.stderr
    assert _extract_identity(recent.stdout) == expected_id
    backups = list((data_dir / "backups").glob("bunny-pre-v2-*.sqlite3"))
    assert len(backups) == 1
    backup_digest = _sha256(backups[0])

    # Inject a synthetic expired session and turn older than 30 days.
    old_ts = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sessions VALUES ('old-sess', ?, ?, ?, 1)",
            (expected_id, old_ts, old_ts),
        )
        conn.execute(
            """
            INSERT INTO turns VALUES (
                'old-turn', 'old-sess', 1,
                'expired turn', 'expired reply', 'offline-simulated', ?
            )
            """,
            (old_ts,),
        )
        conn.commit()
    finally:
        conn.close()

    pre_status_digest = _sha256(db_path)
    status_res = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert status_res.returncode == 0, status_res.stderr
    assert "Saved turns: 2" in status_res.stdout
    assert _sha256(db_path) == pre_status_digest

    # Writable --no-history startup must prune the expired turn without saving the new one.
    no_hist = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--no-history", "-m", "ephemeral turn"],
        env=env,
        cwd=tmp_path,
    )
    assert no_hist.returncode == 0, no_hist.stderr
    assert _extract_identity(no_hist.stdout) == expected_id

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT identity_id FROM identity").fetchone() == (
            expected_id,
        )
        turns = conn.execute(
            "SELECT user_text FROM turns ORDER BY created_at"
        ).fetchall()
        assert turns == [("recent turn",)]
        sessions = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = 'old-sess'"
        ).fetchall()
        assert sessions == []
    finally:
        conn.close()

    assert _sha256(backups[0]) == backup_digest


def _spawn_hot_journal_crash(db_path: Path, *, statements: list[str]) -> Path:
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


def test_cli_hot_journal_status_guidance_and_chat_recovery(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "cli-hot-journal"

    first = _run_cli(
        [
            "chat",
            "--data-dir",
            str(data_dir),
            "--history",
            "--skip-intro",
            "-m",
            "committed before crash",
        ],
        env=env,
        cwd=tmp_path,
    )
    assert first.returncode == 0, first.stderr
    expected_id = _extract_identity(first.stdout)
    db_path = data_dir / DB_FILENAME

    crash_statements = [
        (
            f"INSERT INTO sessions VALUES ('crash-sess', '{expected_id}', "
            "'2026-11-25T00:00:00Z', NULL, 1)"
        ),
        *[
            (
                f"INSERT INTO turns VALUES ('crash-turn-{idx}', 'crash-sess', {idx}, "
                f"'{('z' * 8192)}', 'uncommitted', 'offline-simulated', '2026-11-25T00:00:00Z')"
            )
            for idx in range(1, 10)
        ],
    ]
    journal_path = _spawn_hot_journal_crash(db_path, statements=crash_statements)
    pre_db_digest = _sha256(db_path)
    pre_journal_digest = _sha256(journal_path)

    # Read-only status gives actionable guidance and does not modify db or journal.
    status_res = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert status_res.returncode == 1
    assert "unrecovered SQLite rollback journal" in status_res.stderr
    assert "bunny chat" in status_res.stderr
    assert "Traceback" not in status_res.stderr
    assert _sha256(db_path) == pre_db_digest
    assert _sha256(journal_path) == pre_journal_digest

    # Writable bunny chat recovers cleanly with the same identity and committed turns.
    resumed = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--history", "-m", "after recovery"],
        env=env,
        cwd=tmp_path,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert _extract_identity(resumed.stdout) == expected_id
    assert "1 prior turn(s) loaded" in resumed.stdout
    assert not journal_path.exists()

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        turns = conn.execute(
            "SELECT user_text FROM turns ORDER BY created_at"
        ).fetchall()
        assert turns == [("committed before crash",), ("after recovery",)]
    finally:
        conn.close()

    # Refusal preservation: foreign and newer-schema DBs with hot journals remain untouched.
    foreign_dir = tmp_path / "foreign-hot"
    foreign_dir.mkdir()
    foreign_db = foreign_dir / DB_FILENAME
    conn = sqlite3.connect(str(foreign_db))
    conn.execute("CREATE TABLE foreign_data (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO foreign_data (body) VALUES ('keep')")
    conn.commit()
    conn.close()
    foreign_journal = _spawn_hot_journal_crash(
        foreign_db,
        statements=[
            f"INSERT INTO foreign_data (body) VALUES ('{('f' * 8192)}')"
            for _ in range(10)
        ],
    )
    f_db_digest = _sha256(foreign_db)
    f_journal_digest = _sha256(foreign_journal)

    foreign_chat = _run_cli(
        ["chat", "--data-dir", str(foreign_dir), "-m", "hello"], env=env, cwd=tmp_path
    )
    assert foreign_chat.returncode == 1
    assert "Storage error:" in foreign_chat.stderr
    assert "Traceback" not in foreign_chat.stderr
    assert _sha256(foreign_db) == f_db_digest
    assert _sha256(foreign_journal) == f_journal_digest

    newer_dir = tmp_path / "newer-hot"
    assert (
        _run_cli(
            ["chat", "--data-dir", str(newer_dir), "--skip-intro", "-m", "init"],
            env=env,
            cwd=tmp_path,
        ).returncode
        == 0
    )
    newer_db = newer_dir / DB_FILENAME
    conn = sqlite3.connect(str(newer_db))
    newer_id = conn.execute("SELECT identity_id FROM identity").fetchone()[0]
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    newer_journal = _spawn_hot_journal_crash(
        newer_db,
        statements=[
            f"INSERT INTO sessions VALUES ('s', '{newer_id}', '2026-11-25T00:00:00Z', NULL, 1)",
            *[
                (
                    f"INSERT INTO turns VALUES ('t-{idx}', 's', {idx}, '{('n' * 8192)}', "
                    "'r', 'offline-simulated', '2026-11-25T00:00:00Z')"
                )
                for idx in range(1, 10)
            ],
        ],
    )
    n_db_digest = _sha256(newer_db)
    n_journal_digest = _sha256(newer_journal)

    newer_chat = _run_cli(
        ["chat", "--data-dir", str(newer_dir), "-m", "hello"], env=env, cwd=tmp_path
    )
    assert newer_chat.returncode == 1
    assert "newer than supported" in newer_chat.stderr
    assert _sha256(newer_db) == n_db_digest
    assert _sha256(newer_journal) == n_journal_digest


def test_cli_interrupted_init_retry_and_unrelated_file_preservation(
    tmp_path: Path,
) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "stale-init"
    data_dir.mkdir()
    stale_tmp = data_dir / f".{DB_FILENAME}.init-deadbeef.tmp"
    stale_journal = data_dir / f".{DB_FILENAME}.init-deadbeef.tmp-journal"
    stale_tmp.write_bytes(b"interrupted init")
    stale_journal.write_bytes(b"interrupted journal")

    status_before = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert status_before.returncode == 0, status_before.stderr
    assert "State: not initialized" in status_before.stdout
    assert not (data_dir / DB_FILENAME).exists()

    first = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--skip-intro", "-m", "retry works"],
        env=env,
        cwd=tmp_path,
    )
    assert first.returncode == 0, first.stderr
    first_id = _extract_identity(first.stdout)
    assert not stale_tmp.exists()
    assert not stale_journal.exists()

    restart = _run_cli(
        ["chat", "--data-dir", str(data_dir), "-m", "second"], env=env, cwd=tmp_path
    )
    assert restart.returncode == 0, restart.stderr
    assert _extract_identity(restart.stdout) == first_id

    # Pre-existing empty or damaged database stays untouched.
    for name, payload in (("empty", b""), ("damaged", b"corrupt bytes")):
        bad_dir = tmp_path / f"preexisting-{name}"
        bad_dir.mkdir()
        bad_db = bad_dir / DB_FILENAME
        bad_db.write_bytes(payload)
        digest = _sha256(bad_db)

        bad_chat = _run_cli(
            ["chat", "--data-dir", str(bad_dir), "-m", "hi"], env=env, cwd=tmp_path
        )
        assert bad_chat.returncode == 1
        assert "Storage error:" in bad_chat.stderr
        assert "Traceback" not in bad_chat.stderr
        assert _sha256(bad_db) == digest


def test_cli_lock_contention_exits_nonzero_without_traceback_and_recovers(
    tmp_path: Path,
) -> None:
    env = _cli_env(tmp_path)
    data_dir = tmp_path / "cli-locked"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text(
        "[storage]\nbusy_timeout_ms = 200\n", encoding="utf-8"
    )

    first = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--skip-intro", "-m", "init"],
        env=env,
        cwd=tmp_path,
    )
    assert first.returncode == 0, first.stderr
    expected_id = _extract_identity(first.stdout)

    holder = sqlite3.connect(str(data_dir / DB_FILENAME), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        locked_status = _run_cli(
            ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
        )
        assert locked_status.returncode == 1
        assert "Storage error:" in locked_status.stderr
        assert "locked" in locked_status.stderr.lower()
        assert "Traceback" not in locked_status.stderr

        locked_chat = _run_cli(
            ["chat", "--data-dir", str(data_dir), "-m", "blocked"],
            env=env,
            cwd=tmp_path,
        )
        assert locked_chat.returncode == 1
        assert "Storage error:" in locked_chat.stderr
        assert "locked" in locked_chat.stderr.lower()
        assert "Traceback" not in locked_chat.stderr
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    after_status = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert after_status.returncode == 0, after_status.stderr
    assert expected_id in after_status.stdout

    after_chat = _run_cli(
        ["chat", "--data-dir", str(data_dir), "-m", "unblocked"], env=env, cwd=tmp_path
    )
    assert after_chat.returncode == 0, after_chat.stderr
    assert _extract_identity(after_chat.stdout) == expected_id
