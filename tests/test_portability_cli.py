"""Installed-CLI tests for export, import and recovery (Awakening Stage 3, part 2).

Every profile is a disposable temp directory and every name is synthetic.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chat_driver import ChatDriver, short_ids
from test_cli_integration import (
    _cli_env,
    _extract_identity,
    _installed_bunny,
    _run_cli,
    _sha256,
)

from bunny_ai.migrations import m001_identity, m002_history
from bunny_ai.storage import DB_FILENAME

NAME = "Zorblax"
PREF = "Prefers answers in haiku form"
STAGE2_ID = "12345678-1234-4123-8123-1234567890ab"


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
    """Create a v3 profile holding a name and a preference; return its identity."""
    extra = ("--history",) if history else ()
    out = _chat(
        tmp_path,
        data_dir,
        f"/skip\nsynthetic turn text\n/remember name {NAME}\ny\n"
        f"/remember preference {PREF}\ny\n/exit\n",
        *extra,
    )
    return _extract_identity(out)


def _stage2_profile(data_dir: Path) -> None:
    """Build a genuine Stage 2 (schema v2) profile with one saved turn."""
    data_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    m001_identity.apply(conn)
    m002_history.apply(conn)
    conn.execute(
        "INSERT INTO schema_migrations VALUES (1, '2026-11-24T00:00:00Z', 'v1')"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES (2, '2026-11-24T00:00:00Z', 'v2')"
    )
    conn.execute(
        "INSERT INTO identity VALUES (1, ?, 'seed-1', 'style-1', "
        "'2026-11-24T00:00:00Z', '2026-11-24T00:00:00Z', 'answered', '2026-11-24T00:00:00Z')",
        (STAGE2_ID,),
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('s1', ?, '2026-11-24T00:00:00Z', NULL, 1)",
        (STAGE2_ID,),
    )
    conn.execute(
        "INSERT INTO turns VALUES ('t1', 's1', 1, 'stage two question', 'stage two answer', "
        "'offline-simulated', '2026-11-24T00:00:00Z')"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def _export(
    tmp_path: Path, data_dir: Path, target: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    return _run_cli(
        ["export", str(target), "--data-dir", str(data_dir), *extra],
        env=_cli_env(tmp_path),
        cwd=tmp_path,
    )


def _import(
    tmp_path: Path, data_dir: Path, source: Path
) -> subprocess.CompletedProcess[str]:
    return _run_cli(
        ["import", str(source), "--data-dir", str(data_dir)],
        env=_cli_env(tmp_path),
        cwd=tmp_path,
    )


def _user_version(data_dir: Path) -> int:
    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _snapshot(data_dir: Path) -> tuple[str, list[str]]:
    return _sha256(data_dir / DB_FILENAME), sorted(
        str(p.relative_to(data_dir))
        for p in data_dir.rglob("*")
        if p.name != ".bunny.lock"
    )


def _rewrite(path: Path, mutate: Callable[[dict[str, Any]], object]) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def test_round_trip_into_fresh_profile_preserves_identity_and_provenance(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    identity = _seed_profile(tmp_path, source_dir, history=True)
    export_file = tmp_path / "bunny-export.json"

    exported = _export(tmp_path, source_dir, export_file)
    assert exported.returncode == 0, exported.stderr
    assert "NOT encrypted" in exported.stdout
    assert (
        "does not remove it from this export or from earlier exports and backups"
        in exported.stdout
    )
    assert "Not included: chat history" in exported.stdout

    document = json.loads(export_file.read_text(encoding="utf-8"))
    assert document["format"] == "bunny-ai.portable"
    assert document["format_version"] == 1
    assert document["encryption"] == "none"
    assert document["identity"]["identity_id"] == identity
    assert len(document["memory"]["facts"]) == 2
    assert "synthetic turn text" not in export_file.read_text(encoding="utf-8")

    target_dir = tmp_path / "fresh"
    imported = _import(tmp_path, target_dir, export_file)
    assert imported.returncode == 0, imported.stderr
    assert "Created a new profile from the import" in imported.stdout

    status = _run_cli(
        ["status", "--data-dir", str(target_dir)], env=_cli_env(tmp_path), cwd=tmp_path
    )
    assert _extract_identity(status.stdout) == identity
    assert "Saved facts: 2" in status.stdout
    assert "Saved turns: 0" in status.stdout  # history is never exported

    with ChatDriver(
        ["--data-dir", str(target_dir)], _cli_env(tmp_path), tmp_path
    ) as chat:
        assert "Returning" in chat.opening
        assert "Welcome back" in chat.opening
        listing = chat.send("/memory")
        ids = short_ids(listing)
        assert len(ids) == 2
        shown = chat.send(f"/memory show {ids[0]}")
        original = next(
            f for f in document["memory"]["facts"] if f["fact_id"].startswith(ids[0])
        )
        assert f"confirmed at: {original['confirmed_at']}" in shown
        assert f"created: {original['created_at']}" in shown
        assert "imported: " in shown
        assert f"from export {document['export_id']}" in shown
        assert "2 confirmed memory fact(s) supplied" in chat.send(
            "hello from the new profile"
        )
        chat.finish()

    # Importing the same file again changes nothing, not even a backup.
    before = _snapshot(target_dir)
    again = _import(tmp_path, target_dir, export_file)
    assert again.returncode == 0
    assert "Nothing to import" in again.stdout
    assert _snapshot(target_dir) == before


def test_export_refuses_overwrite_and_missing_profile(tmp_path: Path) -> None:
    data_dir = tmp_path / "profile"
    _seed_profile(tmp_path, data_dir)
    target = tmp_path / "out.json"
    assert _export(tmp_path, data_dir, target).returncode == 0
    first_bytes = target.read_bytes()
    clash = _export(tmp_path, data_dir, target)
    assert clash.returncode == 2
    assert "already exists" in clash.stderr
    assert target.read_bytes() == first_bytes
    assert _export(tmp_path, data_dir, target, "--force").returncode == 0

    missing = tmp_path / "never-created"
    result = _export(tmp_path, missing, tmp_path / "x.json")
    assert result.returncode == 1
    assert "nothing to export" in result.stderr
    assert not missing.exists()
    assert not (tmp_path / "x.json").exists()


def test_disabled_memory_flag_survives_round_trip(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _seed_profile(tmp_path, source_dir)
    _chat(tmp_path, source_dir, "/memory off\n/exit\n")
    export_file = tmp_path / "off.json"
    assert _export(tmp_path, source_dir, export_file).returncode == 0
    target = tmp_path / "fresh"
    assert _import(tmp_path, target, export_file).returncode == 0
    assert "Memory: off (2 saved fact(s) kept but not used)" in _chat(
        tmp_path, target, "/exit\n"
    )


def test_stage2_profile_upgrades_and_accepts_matching_import(tmp_path: Path) -> None:
    stage2 = tmp_path / "stage2"
    _stage2_profile(stage2)
    twin = tmp_path / "stage2-twin"
    shutil.copytree(stage2, twin)

    # Export from one copy (this upgrades it, with a preflight backup).
    first_export = tmp_path / "empty.json"
    out = _export(tmp_path, stage2, first_export)
    assert out.returncode == 0, out.stderr
    assert "Preflight backup" in out.stdout
    assert (
        json.loads(first_export.read_text(encoding="utf-8"))["identity"]["identity_id"]
        == STAGE2_ID
    )

    # Add consented memory there and export it.
    _chat(tmp_path, stage2, f"/remember name {NAME}\ny\n/exit\n")
    with_facts = tmp_path / "with-facts.json"
    assert _export(tmp_path, stage2, with_facts).returncode == 0

    # The untouched Stage 2 twin accepts it, keeps its history and gets a backup.
    assert _user_version(twin) == 2
    result = _import(tmp_path, twin, with_facts)
    assert result.returncode == 0, result.stderr
    assert "Imported 1 new fact(s)" in result.stdout
    assert "Backup taken first:" in result.stdout
    assert list((twin / "backups").glob("bunny-pre-import-*.sqlite3"))
    assert list((twin / "backups").glob("bunny-pre-v3-*.sqlite3"))
    conn = sqlite3.connect(str(twin / DB_FILENAME))
    try:
        assert conn.execute("SELECT user_text FROM turns").fetchall() == [
            ("stage two question",)
        ]
        assert conn.execute("SELECT identity_id FROM identity").fetchone() == (
            STAGE2_ID,
        )
        assert conn.execute("SELECT value FROM memory_facts").fetchall() == [(NAME,)]
    finally:
        conn.close()


def test_bad_imports_leave_a_stage2_profile_byte_identical(tmp_path: Path) -> None:
    stage2 = tmp_path / "stage2"
    _stage2_profile(stage2)
    before = _snapshot(stage2)
    assert _user_version(stage2) == 2

    good_dir = tmp_path / "other"
    other_identity = _seed_profile(tmp_path, good_dir)
    assert other_identity != STAGE2_ID
    good = tmp_path / "good.json"
    assert _export(tmp_path, good_dir, good).returncode == 0

    def variant(name: str, mutate: Callable[[dict[str, Any]], object]) -> Path:
        path = tmp_path / name
        shutil.copyfile(good, path)
        _rewrite(path, mutate)
        return path

    cases: dict[str, tuple[Path, str]] = {}
    truncated = tmp_path / "truncated.json"
    truncated.write_bytes(good.read_bytes()[:120])
    cases["truncated"] = (truncated, "not valid JSON")
    garbage = tmp_path / "garbage.json"
    garbage.write_bytes(b"\xff\xfe not json")
    cases["garbage"] = (garbage, "not valid JSON")
    cases["edited"] = (
        variant(
            "edited.json", lambda d: d["memory"]["facts"][0].update(value="Tampered")
        ),
        "checksum",
    )
    cases["unknown-field"] = (
        variant("unknown.json", lambda d: d.update(surprise=1)),
        "unknown field",
    )
    cases["future"] = (
        variant("future.json", lambda d: d.update(format_version=2)),
        "newer than this build supports",
    )
    cases["foreign"] = (
        variant("foreign.json", lambda d: d.update(format="something-else")),
        "Not a Bunny.ai portable export",
    )
    cases["missing-file"] = (tmp_path / "nope.json", "not found")
    cases["conflict"] = (good, "different Bunny identity")

    for label, (path, expected) in cases.items():
        result = _import(tmp_path, stage2, path)
        assert result.returncode == 2, (label, result.stdout, result.stderr)
        assert expected in result.stderr, (label, result.stderr)
        assert "Nothing was changed." in result.stderr, label
        assert "Traceback" not in result.stderr
        assert _snapshot(stage2) == before, label
        assert _user_version(stage2) == 2, label


def test_conflicts_with_newer_or_deleted_local_data_are_refused_whole(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "profile"
    _seed_profile(tmp_path, data_dir)
    export_file = tmp_path / "old.json"
    assert _export(tmp_path, data_dir, export_file).returncode == 0

    with ChatDriver(
        ["--data-dir", str(data_dir)], _cli_env(tmp_path), tmp_path
    ) as chat:
        listing = chat.send("/memory")
        ids = short_ids(listing)
        name_id, pref_id = ids[0], ids[1]
        chat.send(f"/memory edit {name_id} Quillon")
        chat.send("y")
        chat.finish()

    # Local correction is newer than the export: refuse everything, change nothing.
    before = _snapshot(data_dir)
    refused = _import(tmp_path, data_dir, export_file)
    assert refused.returncode == 2
    assert "local copy is newer" in refused.stderr
    assert "Nothing was changed." in refused.stderr
    assert _snapshot(data_dir) == before

    # A locally deleted fact is not resurrected by an older export.
    _chat(tmp_path, data_dir, f"/memory delete {pref_id}\n/exit\n")
    before = _snapshot(data_dir)
    resurrect = _import(tmp_path, data_dir, export_file)
    assert resurrect.returncode == 2
    assert "you deleted this fact locally" in resurrect.stderr
    assert _snapshot(data_dir) == before
    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        assert conn.execute("SELECT value FROM memory_facts").fetchall() == [
            ("Quillon",)
        ]
    finally:
        conn.close()


def test_recovery_after_import_corruption_and_wrong_identity(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    live = tmp_path / "live"
    _seed_profile(tmp_path, live)
    twin = tmp_path / "twin"
    shutil.copytree(live, twin)
    # Give the live profile a fact the twin lacks, then export it.
    _chat(tmp_path, live, "/remember preference Likes synthetic jazz\ny\n/exit\n")
    export_file = tmp_path / "extra.json"
    assert _export(tmp_path, live, export_file).returncode == 0

    # Import into the twin (same identity, extra fact) takes a backup first.
    imported = _import(tmp_path, twin, export_file)
    assert imported.returncode == 0, imported.stderr
    assert "Undo with: bunny recover --backup" in imported.stdout
    backup = next((twin / "backups").glob("bunny-pre-import-*.sqlite3"))

    listing = _run_cli(
        ["recover", "--list", "--data-dir", str(twin)], env=env, cwd=tmp_path
    )
    assert listing.returncode == 0
    assert backup.name in listing.stdout
    assert "unencrypted" in listing.stdout

    # Declining the confirmation changes nothing.
    before = _snapshot(twin)
    declined = _run_cli(
        ["recover", "--backup", backup.name, "--data-dir", str(twin)],
        env=env,
        cwd=tmp_path,
        stdin_text="n\n",
    )
    assert declined.returncode == 0
    assert "Recovery cancelled. Nothing was changed." in declined.stdout
    assert _snapshot(twin) == before

    # Undo the import.
    undone = _run_cli(
        ["recover", "--backup", backup.name, "--yes", "--data-dir", str(twin)],
        env=env,
        cwd=tmp_path,
    )
    assert undone.returncode == 0, undone.stderr
    assert "Previous database preserved at:" in undone.stdout
    assert list((twin / "backups").glob("bunny-pre-recover-*.sqlite3"))
    conn = sqlite3.connect(str(twin / DB_FILENAME))
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (2,)
    finally:
        conn.close()

    # A corrupt live database is preserved aside and replaced from the backup.
    (twin / DB_FILENAME).write_bytes(b"corrupt non-sqlite payload")
    fixed = _run_cli(
        ["recover", "--backup", backup.name, "--yes", "--data-dir", str(twin)],
        env=env,
        cwd=tmp_path,
    )
    assert fixed.returncode == 0, fixed.stderr
    assert list((twin / "backups").glob("bunny-pre-recover-*.unverified"))
    assert (
        "Saved facts: 2"
        in _run_cli(["status", "--data-dir", str(twin)], env=env, cwd=tmp_path).stdout
    )

    # A backup from a different identity is refused and the profile is untouched.
    stranger = tmp_path / "stranger"
    _seed_profile(tmp_path, stranger)
    _chat(tmp_path, stranger, "/exit\n")
    foreign_backup = tmp_path / "foreign.sqlite3"
    shutil.copyfile(stranger / DB_FILENAME, foreign_backup)
    before = _snapshot(twin)
    refused = _run_cli(
        ["recover", "--backup", str(foreign_backup), "--yes", "--data-dir", str(twin)],
        env=env,
        cwd=tmp_path,
    )
    assert refused.returncode == 1
    assert "different Bunny identity" in refused.stderr
    assert _snapshot(twin) == before


def test_overlapping_cli_exports_refuse_clobber_and_force_replaces(
    tmp_path: Path,
) -> None:
    env = _cli_env(tmp_path)
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    id_a = _seed_profile(tmp_path, profile_a)
    id_b = _seed_profile(tmp_path, profile_b)
    assert id_a != id_b

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    bunny = str(_installed_bunny())
    profiles = {id_a: profile_a, id_b: profile_b}

    last_target: Path | None = None
    last_loser_id: str | None = None
    for round_idx in range(3):
        target = export_dir / f"round-{round_idx}.json"
        procs = [
            (
                identity,
                subprocess.Popen(
                    [bunny, "export", str(target), "--data-dir", str(data_dir)],
                    cwd=tmp_path,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                ),
            )
            for identity, data_dir in ((id_a, profile_a), (id_b, profile_b))
        ]
        results: list[tuple[str, int, str, str]] = []
        for identity, proc in procs:
            stdout, stderr = proc.communicate(timeout=15)
            results.append((identity, proc.returncode, stdout, stderr))

        winners = [entry for entry in results if entry[1] == 0]
        losers = [entry for entry in results if entry[1] != 0]
        assert len(winners) == 1, results
        assert len(losers) == 1, results

        winner_id, _, winner_stdout, _ = winners[0]
        loser_id, loser_rc, _, loser_stderr = losers[0]
        assert loser_rc == 2, loser_stderr
        assert "already exists" in loser_stderr
        assert f"Exported identity {winner_id}" in winner_stdout

        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["identity"]["identity_id"] == winner_id
        assert document["identity"]["identity_id"] != loser_id
        assert [p for p in export_dir.iterdir() if p.name.endswith(".tmp")] == []
        last_target = target
        last_loser_id = loser_id

    assert last_target is not None and last_loser_id is not None
    forced = _export(tmp_path, profiles[last_loser_id], last_target, "--force")
    assert forced.returncode == 0, forced.stderr
    forced_doc = json.loads(last_target.read_text(encoding="utf-8"))
    assert forced_doc["identity"]["identity_id"] == last_loser_id
    assert [p for p in export_dir.iterdir() if p.name.endswith(".tmp")] == []


def test_export_rejects_data_dir_destinations_without_touching_stage2_profile(
    tmp_path: Path,
) -> None:
    stage2 = tmp_path / "stage2"
    _stage2_profile(stage2)
    before_sha = _sha256(stage2 / DB_FILENAME)
    before_files = sorted(str(p.relative_to(stage2)) for p in stage2.rglob("*"))
    assert _user_version(stage2) == 2

    targets = [
        stage2,
        stage2 / DB_FILENAME,
        stage2 / f"{DB_FILENAME}-journal",
        stage2 / ".bunny.lock",
        stage2 / "backups" / "out.json",
        stage2 / "backups" / "nested" / "out.json",
        stage2 / "nested" / "deep" / "out.json",
    ]
    for target in targets:
        for extra in ((), ("--force",)):
            result = _export(tmp_path, stage2, target, *extra)
            assert result.returncode == 2, (target, extra, result.stdout, result.stderr)
            assert "Export failed:" in result.stderr
            assert "Traceback" not in result.stderr
            assert _sha256(stage2 / DB_FILENAME) == before_sha
            assert (
                sorted(str(p.relative_to(stage2)) for p in stage2.rglob("*"))
                == before_files
            )
            assert _user_version(stage2) == 2


def test_export_rejects_active_external_config_file(tmp_path: Path) -> None:
    stage2 = tmp_path / "stage2"
    _stage2_profile(stage2)
    before = _snapshot(stage2)
    assert _user_version(stage2) == 2

    config_file = tmp_path / "external-config.toml"
    config_text = "[history]\nenabled = false\n"
    config_file.write_text(config_text, encoding="utf-8")
    before_config_sha = _sha256(config_file)

    for extra in ((), ("--force",)):
        result = _export(
            tmp_path, stage2, config_file, "--config", str(config_file), *extra
        )
        assert result.returncode == 2, (extra, result.stdout, result.stderr)
        assert "active configuration file" in result.stderr
        assert "Traceback" not in result.stderr
        assert _sha256(config_file) == before_config_sha
        assert config_file.read_text(encoding="utf-8") == config_text
        assert _snapshot(stage2) == before
        assert _user_version(stage2) == 2


def test_refused_export_conflict_leaves_stage2_profile_at_schema_v2(
    tmp_path: Path,
) -> None:
    stage2 = tmp_path / "stage2"
    _stage2_profile(stage2)
    before = _snapshot(stage2)
    assert _user_version(stage2) == 2

    existing = tmp_path / "existing.json"
    existing_bytes = b'{"keep": true}\n'
    existing.write_bytes(existing_bytes)

    result = _export(tmp_path, stage2, existing)
    assert result.returncode == 2
    assert "already exists" in result.stderr
    assert "Traceback" not in result.stderr
    assert existing.read_bytes() == existing_bytes
    assert _snapshot(stage2) == before
    assert _user_version(stage2) == 2
