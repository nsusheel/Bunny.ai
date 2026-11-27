"""Unit tests for the export format, validation and conflict planning."""

from __future__ import annotations

import errno
import json
import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bunny_ai import portability
from bunny_ai.config import restrict_path_permissions as real_restrict
from bunny_ai.errors import ExportError, ImportValidationError, UnsupportedImportError
from bunny_ai.memory import MemoryFact
from bunny_ai.portability import (
    FORMAT_VERSION,
    build_export,
    parse_import,
    plan_import,
    validate_export_destination,
    write_export_file,
)
from bunny_ai.storage import IdentityRecord

TS = "2026-11-25T00:00:00.000000Z"
IDENTITY = IdentityRecord(
    identity_id=str(uuid.uuid4()),
    seed_version="seed-1",
    style_version="style-1",
    created_at=TS,
    awakened_at=TS,
    onboarding_state="skipped",
    updated_at=TS,
)


def _fact(category: str = "preference", value: str = "synthetic") -> MemoryFact:
    return MemoryFact(
        fact_id=str(uuid.uuid4()),
        identity_id=IDENTITY.identity_id,
        category=category,  # type: ignore[arg-type]
        value=value,
        source="explicit_command",
        confirmation="user_confirmed_local",
        created_at=TS,
        updated_at=TS,
        confirmed_at=TS,
        correction_count=0,
        sensitivity="normal",
        retention="until_deleted",
        imported_at=None,
        imported_from=None,
    )


def _export(*facts: MemoryFact) -> bytes:
    return build_export(
        IDENTITY, memory_enabled=True, facts=facts, bunny_version="0", exported_at=TS
    )


def test_round_trip_through_parse() -> None:
    fact = _fact("preferred_name", "Zorblax")
    bundle = parse_import(_export(fact))
    assert bundle.identity == IDENTITY
    assert bundle.facts == (fact,)
    assert bundle.memory_enabled is True


def test_format_is_versioned_and_stable_keys() -> None:
    document: dict[str, Any] = json.loads(_export(_fact()))
    assert document["format_version"] == FORMAT_VERSION == 1
    assert set(document) == {
        "format",
        "format_version",
        "export_id",
        "exported_at",
        "bunny_ai_version",
        "encryption",
        "notice",
        "identity",
        "memory",
        "integrity",
    }


def test_validation_reports_every_problem_before_anything_is_applied() -> None:
    document: dict[str, Any] = json.loads(_export(_fact(), _fact(value="other")))
    document["memory"]["facts"][0]["category"] = "secret"
    document["memory"]["facts"][1]["value"] = "line\nbreak"
    document["identity"]["identity_id"] = "not-a-uuid"
    with pytest.raises(ImportValidationError) as raised:
        parse_import(json.dumps(document).encode())
    text = " ".join(raised.value.problems)
    assert "category" in text
    assert "single line" in text
    assert "identity_id" in text


def test_unsupported_versions_and_formats() -> None:
    document: dict[str, Any] = json.loads(_export())
    document["format_version"] = 2
    with pytest.raises(UnsupportedImportError):
        parse_import(json.dumps(document).encode())
    document["format_version"] = 0
    with pytest.raises(ImportValidationError):
        parse_import(json.dumps(document).encode())
    document["format"] = "other"
    with pytest.raises(UnsupportedImportError):
        parse_import(json.dumps(document).encode())
    with pytest.raises(ImportValidationError):
        parse_import(b'{"format": "bunny-ai.portable", "format_version": NaN}')
    with pytest.raises(ImportValidationError):
        parse_import(b"x" * 1_048_577)


def test_plan_import_rules() -> None:
    mine = _fact("preferred_name", "Zorblax")
    incoming_name = _fact("preferred_name", "Quillon")
    new_pref = _fact(value="brand new")
    bundle = parse_import(_export(incoming_name, new_pref))

    plan = plan_import(
        bundle,
        local_identity_id=IDENTITY.identity_id,
        local_facts=(mine,),
        tombstones={},
    )
    assert plan.add == (new_pref,)
    assert len(plan.conflicts) == 1 and "different preferred name" in plan.conflicts[0]

    same = plan_import(
        parse_import(_export(mine)),
        local_identity_id=IDENTITY.identity_id,
        local_facts=(mine,),
        tombstones={},
    )
    assert same.add == () and same.unchanged == 1 and same.conflicts == ()

    newer_local = replace(
        mine, value="Changed", updated_at="2026-12-01T00:00:00.000000Z"
    )
    conflict = plan_import(
        parse_import(_export(mine)),
        local_identity_id=IDENTITY.identity_id,
        local_facts=(newer_local,),
        tombstones={},
    )
    assert "local copy is newer" in conflict.conflicts[0]

    deleted = plan_import(
        parse_import(_export(mine)),
        local_identity_id=IDENTITY.identity_id,
        local_facts=(),
        tombstones={mine.fact_id: TS},
    )
    assert "deleted this fact locally" in deleted.conflicts[0]

    other = plan_import(
        parse_import(_export(mine)),
        local_identity_id=str(uuid.uuid4()),
        local_facts=(),
        tombstones={},
    )
    assert "different Bunny identity" in other.conflicts[0]


def test_write_export_file_overlapping_writers_no_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "race.json"
    payloads = (b'{"writer": "one"}\n', b'{"writer": "two"}\n')
    barrier = threading.Barrier(2)

    def synced_restrict(path: Path, *, is_dir: bool) -> None:
        real_restrict(path, is_dir=is_dir)
        barrier.wait(timeout=5)

    monkeypatch.setattr(portability, "restrict_path_permissions", synced_restrict)

    outcomes: list[tuple[bytes, ExportError | None]] = []
    unexpected: list[BaseException] = []
    lock = threading.Lock()

    def worker(payload: bytes) -> None:
        try:
            write_export_file(target, payload, force=False)
            with lock:
                outcomes.append((payload, None))
        except ExportError as exc:
            with lock:
                outcomes.append((payload, exc))
        except BaseException as exc:  # pragma: no cover - defensive failure capture
            with lock:
                unexpected.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert not unexpected
    winners = [payload for payload, err in outcomes if err is None]
    losers = [err for _, err in outcomes if err is not None]
    assert len(winners) == 1
    assert len(losers) == 1
    assert "already exists" in str(losers[0])
    assert target.read_bytes() == winners[0]
    assert list(tmp_path.iterdir()) == [target]


def test_write_export_file_posix_link_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "posix.json"
    payload = b'{"posix": true}\n'
    linked: list[tuple[str, str]] = []

    def fake_link(src: str | Path, dst: str | Path) -> None:
        linked.append((os.fspath(src), os.fspath(dst)))
        Path(dst).write_bytes(Path(src).read_bytes())

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "link", fake_link)

    write_export_file(target, payload, force=False)
    assert len(linked) == 1
    assert linked[0][1] == os.fspath(target)
    assert target.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [target]


def test_write_export_file_posix_link_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "posix-conflict.json"
    existing = b'{"existing": true}\n'

    def racing_link(src: str | Path, dst: str | Path) -> None:
        Path(dst).write_bytes(existing)
        raise FileExistsError(errno.EEXIST, "File exists", str(dst))

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(ExportError, match="already exists"):
        write_export_file(target, b'{"new": true}\n', force=False)
    assert target.read_bytes() == existing
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("link_errno", [errno.ENOTSUP, errno.EPERM])
def test_write_export_file_posix_unsupported_link_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_errno: int
) -> None:
    monkeypatch.setattr(os, "name", "posix")

    target = tmp_path / "unsupported-link.json"

    def unsupported_link(src: str | Path, dst: str | Path) -> None:
        raise OSError(link_errno, os.strerror(link_errno))

    monkeypatch.setattr(os, "link", unsupported_link)
    with pytest.raises(ExportError, match="atomic no-overwrite"):
        write_export_file(target, b'{"fallback": false}\n', force=False)
    assert not target.exists()
    assert not os.path.lexists(target)
    assert list(tmp_path.iterdir()) == []


def test_write_export_file_force_replaces_existing(tmp_path: Path) -> None:
    target = tmp_path / "replace.json"
    target.write_bytes(b'{"old": true}\n')
    replacement = b'{"new": true}\n'

    write_export_file(target, replacement, force=True)
    assert target.read_bytes() == replacement
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "relative_target",
    [
        Path("."),
        Path("bunny.sqlite3"),
        Path("backups/x.json"),
        Path("backups/deep/x.json"),
        Path("sub/deep/x.json"),
    ],
)
@pytest.mark.parametrize("force", [False, True])
def test_validate_export_destination_rejects_data_dir_at_any_depth(
    tmp_path: Path, relative_target: Path, force: bool
) -> None:
    data_dir = tmp_path / "profile"
    data_dir.mkdir()
    target = data_dir if relative_target == Path(".") else data_dir / relative_target

    with pytest.raises(ExportError, match="data directory"):
        validate_export_destination(
            target, data_dir=data_dir, config_path=None, force=force
        )


@pytest.mark.parametrize("force", [False, True])
def test_validate_export_destination_rejects_external_config_path(
    tmp_path: Path, force: bool
) -> None:
    data_dir = tmp_path / "profile"
    data_dir.mkdir()
    config_file = tmp_path / "bunny.toml"
    config_file.write_text("[history]\nenabled = false\n", encoding="utf-8")

    with pytest.raises(ExportError, match="active configuration file"):
        validate_export_destination(
            config_file, data_dir=data_dir, config_path=config_file, force=force
        )


def test_validate_export_destination_checks_parent_dir_type_and_conflicts(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "profile"
    data_dir.mkdir()
    export_dir = tmp_path / "exports"
    export_dir.mkdir()

    with pytest.raises(ExportError, match="NUL byte"):
        validate_export_destination(
            export_dir / "bad\x00name.json",
            data_dir=data_dir,
            config_path=None,
            force=False,
        )

    with pytest.raises(ExportError, match="does not exist"):
        validate_export_destination(
            tmp_path / "missing-parent" / "out.json",
            data_dir=data_dir,
            config_path=None,
            force=False,
        )

    subdir = export_dir / "folder"
    subdir.mkdir()
    with pytest.raises(ExportError, match="is a directory"):
        validate_export_destination(
            subdir, data_dir=data_dir, config_path=None, force=False
        )

    existing = export_dir / "existing.json"
    existing.write_bytes(b"{}\n")
    with pytest.raises(ExportError, match="already exists"):
        validate_export_destination(
            existing, data_dir=data_dir, config_path=None, force=False
        )

    assert (
        validate_export_destination(
            existing, data_dir=data_dir, config_path=None, force=True
        )
        == existing.resolve()
    )
    fresh = export_dir / "fresh.json"
    assert (
        validate_export_destination(
            fresh, data_dir=data_dir, config_path=None, force=False
        )
        == fresh.resolve()
    )
