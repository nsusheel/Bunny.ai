"""Versioned local export and import of identity and consented memory.

Format ``bunny-ai.portable`` version 1 (see docs/PORTABILITY.md). Exports are
plain, unencrypted JSON. They contain the identity record and the durable facts
the user confirmed; they never contain chat history, backups, tombstones or
configuration. Imports are validated completely before any local data is
touched, and any conflict with local data refuses the whole import.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from bunny_ai.config import restrict_path_permissions
from bunny_ai.errors import (
    ExportError,
    ImportConflictError,
    ImportValidationError,
    UnsupportedImportError,
)
from bunny_ai.memory import (
    CATEGORIES,
    CONFIRMATION_LOCAL,
    FACT_COLUMNS,
    MAX_FACTS,
    SOURCE_EXPLICIT,
    InvalidFactError,
    MemoryFact,
    MemoryStore,
    validate_fact_value,
)
from bunny_ai.storage import IdentityRecord, LocalStorage

FORMAT_NAME = "bunny-ai.portable"
FORMAT_VERSION = 1
MAX_IMPORT_BYTES = 1_048_576

UNENCRYPTED_NOTICE = (
    "This export is NOT encrypted. Anyone who can read the file can read your "
    "identity id and saved facts. Store it somewhere you trust."
)
DELETION_NOTICE = (
    "Deleting a fact from your local profile later does not remove it from this "
    "export or from earlier exports and backups."
)
FILE_NOTICE = f"{UNENCRYPTED_NOTICE} {DELETION_NOTICE}"

_TOP_KEYS = frozenset(
    {
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
)
_IDENTITY_KEYS = frozenset(
    {
        "identity_id",
        "seed_version",
        "style_version",
        "created_at",
        "awakened_at",
        "onboarding_state",
        "updated_at",
    }
)
_MEMORY_KEYS = frozenset({"enabled", "facts"})
_FACT_KEYS = frozenset(
    {
        "fact_id",
        "category",
        "value",
        "source",
        "confirmation",
        "created_at",
        "updated_at",
        "confirmed_at",
        "correction_count",
        "sensitivity",
        "retention",
        "imported_at",
        "imported_from",
    }
)
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


@dataclass(frozen=True)
class ImportBundle:
    """A fully validated import file."""

    export_id: str
    exported_at: str
    identity: IdentityRecord
    memory_enabled: bool
    facts: tuple[MemoryFact, ...]


@dataclass(frozen=True)
class ImportPlan:
    """What an import would do against the current local profile."""

    add: tuple[MemoryFact, ...]
    unchanged: int
    conflicts: tuple[str, ...]


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _checksum(identity: dict[str, Any], memory: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical({"identity": identity, "memory": memory})
    ).hexdigest()


def build_export(
    identity: IdentityRecord,
    *,
    memory_enabled: bool,
    facts: tuple[MemoryFact, ...],
    bunny_version: str,
    exported_at: str,
) -> bytes:
    """Serialize identity and consented memory as export format version 1."""
    identity_doc: dict[str, Any] = {
        "identity_id": identity.identity_id,
        "seed_version": identity.seed_version,
        "style_version": identity.style_version,
        "created_at": identity.created_at,
        "awakened_at": identity.awakened_at,
        "onboarding_state": identity.onboarding_state,
        "updated_at": identity.updated_at,
    }
    memory_doc: dict[str, Any] = {
        "enabled": memory_enabled,
        "facts": [
            {
                "fact_id": f.fact_id,
                "category": f.category,
                "value": f.value,
                "source": f.source,
                "confirmation": f.confirmation,
                "created_at": f.created_at,
                "updated_at": f.updated_at,
                "confirmed_at": f.confirmed_at,
                "correction_count": f.correction_count,
                "sensitivity": f.sensitivity,
                "retention": f.retention,
                "imported_at": f.imported_at,
                "imported_from": f.imported_from,
            }
            for f in facts
        ],
    }
    document = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "export_id": str(uuid.uuid4()),
        "exported_at": exported_at,
        "bunny_ai_version": bunny_version,
        "encryption": "none",
        "notice": FILE_NOTICE,
        "identity": identity_doc,
        "memory": memory_doc,
        "integrity": {"sha256": _checksum(identity_doc, memory_doc)},
    }
    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


_LINK_UNSUPPORTED_ERRNOS = frozenset(
    {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.ENOSYS}
)


def _export_conflict_error(path: Path) -> ExportError:
    return ExportError(
        f"'{path}' already exists. Choose another path or pass --force to replace it."
    )


def _resolve_path(path: Path, raw_text: str) -> Path:
    try:
        return path.resolve()
    except OSError as exc:
        raise ExportError(f"Invalid export path '{raw_text}': {exc}") from exc


def validate_export_destination(
    path: Path,
    *,
    data_dir: Path,
    config_path: Path | None,
    force: bool,
) -> Path:
    """Validate an export target before touching the local profile."""
    raw_text = str(path)
    if "\x00" in raw_text:
        raise ExportError("Export path contains an invalid NUL byte.")
    raw = path.expanduser()
    resolved_data_dir = data_dir.expanduser().resolve()
    resolved_target = _resolve_path(raw, raw_text)
    parent_dir = _resolve_path(raw.parent, raw_text)
    parent_resolved_target = parent_dir / raw.name
    candidates = (resolved_target, parent_resolved_target)

    for candidate in candidates:
        if candidate == resolved_data_dir or candidate.is_relative_to(
            resolved_data_dir
        ):
            raise ExportError(
                f"Do not export inside the Bunny.ai data directory '{resolved_data_dir}'; "
                "choose a location outside the profile."
            )

    if config_path is not None:
        resolved_config = config_path.expanduser().resolve()
        for candidate in candidates:
            if candidate == resolved_config:
                raise ExportError(
                    f"Do not overwrite the active configuration file '{resolved_config}'; "
                    "choose another path."
                )

    if raw.is_dir() or resolved_target.is_dir():
        raise ExportError(f"Export path is a directory: {resolved_target}")
    if not parent_dir.is_dir():
        raise ExportError(f"Export folder does not exist: {raw.parent}")
    if not force and os.path.lexists(raw):
        raise _export_conflict_error(resolved_target)
    return resolved_target


def _publish_no_clobber(temp: Path, path: Path) -> None:
    """Atomically publish ``temp`` to ``path`` without overwriting an existing entry."""
    try:
        if os.name == "nt":
            # Windows rename refuses to overwrite an existing target atomically.
            os.rename(temp, path)
            return
        os.link(temp, path)
    except FileExistsError as exc:
        raise _export_conflict_error(path) from exc
    except OSError as exc:
        if os.name != "nt" and exc.errno in _LINK_UNSUPPORTED_ERRNOS:
            raise ExportError(
                f"Cannot safely publish export '{path}' without overwriting because "
                "this filesystem does not support atomic no-overwrite linking. "
                "Choose another directory or pass --force to replace an existing file."
            ) from exc
        raise


def write_export_file(path: Path, payload: bytes, *, force: bool) -> None:
    """Write the export atomically with owner-only mode where the OS supports it."""
    if "\x00" in str(path):
        raise ExportError("Export path contains an invalid NUL byte.")
    if path.is_dir():
        raise ExportError(f"Export path is a directory: {path}")
    if not path.parent.is_dir():
        raise ExportError(f"Export folder does not exist: {path.parent}")
    if not force and os.path.lexists(path):
        raise _export_conflict_error(path)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        restrict_path_permissions(temp, is_dir=False)
        if force:
            os.replace(temp, path)
        else:
            _publish_no_clobber(temp, path)
    except OSError as exc:
        raise ExportError(f"Could not write export '{path}': {exc}") from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _is_nonempty_text(value: object, limit: int = 64) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-standard JSON constant {name}")


def _check_keys(
    label: str, doc: dict[str, Any], expected: frozenset[str], problems: list[str]
) -> bool:
    missing = sorted(expected - doc.keys())
    extra = sorted(doc.keys() - expected)
    if missing:
        problems.append(f"{label}: missing field(s) {', '.join(missing)}")
    if extra:
        problems.append(f"{label}: unknown field(s) {', '.join(extra)}")
    return not missing and not extra


def parse_import(raw: bytes) -> ImportBundle:
    """Validate an entire import file without touching any profile.

    Raises ``ImportValidationError`` listing every problem found, or
    ``UnsupportedImportError`` for another format or a newer version.
    """
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportValidationError(
            f"Import file is larger than {MAX_IMPORT_BYTES} bytes."
        )
    try:
        document = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ImportValidationError(f"Import file is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ImportValidationError("Import file must contain a JSON object.")
    doc = cast(dict[str, Any], document)
    if doc.get("format") != FORMAT_NAME:
        raise UnsupportedImportError(
            f"Not a Bunny.ai portable export (expected format '{FORMAT_NAME}')."
        )
    version = doc.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ImportValidationError("format_version must be an integer.")
    if version > FORMAT_VERSION:
        raise UnsupportedImportError(
            f"Export format version {version} is newer than this build supports "
            f"(version {FORMAT_VERSION}). Upgrade Bunny.ai; nothing was imported."
        )
    if version < 1:
        raise ImportValidationError(f"Invalid export format version {version}.")

    problems: list[str] = []
    top_ok = _check_keys("export", doc, _TOP_KEYS, problems)
    if not top_ok:
        raise ImportValidationError("Import file is malformed.", tuple(problems))
    if not _is_uuid(doc["export_id"]):
        problems.append("export_id must be a canonical lowercase UUID")
    if not _is_timestamp(doc["exported_at"]):
        problems.append("exported_at must be a UTC timestamp")
    if doc["encryption"] != "none":
        problems.append("encryption must be 'none' in format version 1")
    if not isinstance(doc["bunny_ai_version"], str) or not doc["bunny_ai_version"]:
        problems.append("bunny_ai_version must be a non-empty string")
    if not isinstance(doc["notice"], str):
        problems.append("notice must be a string")

    identity_doc = doc["identity"]
    memory_doc = doc["memory"]
    integrity = doc["integrity"]
    identity: IdentityRecord | None = None
    if not isinstance(identity_doc, dict) or not _check_keys(
        "identity", identity_doc, _IDENTITY_KEYS, problems
    ):
        if not isinstance(identity_doc, dict):
            problems.append("identity must be an object")
    else:
        identity = _validate_identity(identity_doc, problems)
    facts: list[MemoryFact] = []
    enabled = True
    if not isinstance(memory_doc, dict) or not _check_keys(
        "memory", memory_doc, _MEMORY_KEYS, problems
    ):
        if not isinstance(memory_doc, dict):
            problems.append("memory must be an object")
    else:
        if not isinstance(memory_doc["enabled"], bool):
            problems.append("memory.enabled must be true or false")
        else:
            enabled = memory_doc["enabled"]
        facts = _validate_facts(memory_doc["facts"], identity, problems)
    if (
        isinstance(integrity, dict)
        and set(integrity) == {"sha256"}
        and isinstance(identity_doc, dict)
        and isinstance(memory_doc, dict)
    ):
        if integrity["sha256"] != _checksum(identity_doc, memory_doc):
            problems.append(
                "integrity checksum does not match the file contents "
                "(the file is corrupt or was edited)"
            )
    else:
        problems.append("integrity must be an object with a single sha256 field")
    if problems or identity is None:
        raise ImportValidationError(
            "Import file failed validation.", tuple(problems[:20])
        )
    return ImportBundle(
        export_id=str(doc["export_id"]),
        exported_at=str(doc["exported_at"]),
        identity=identity,
        memory_enabled=enabled,
        facts=tuple(facts),
    )


def _validate_identity(
    doc: dict[str, Any], problems: list[str]
) -> IdentityRecord | None:
    before = len(problems)
    if not _is_uuid(doc["identity_id"]):
        problems.append("identity.identity_id must be a canonical lowercase UUID")
    for key in ("seed_version", "style_version"):
        if not _is_nonempty_text(doc[key]):
            problems.append(f"identity.{key} must be short non-empty text")
    for key in ("created_at", "awakened_at", "updated_at"):
        if not _is_timestamp(doc[key]):
            problems.append(f"identity.{key} must be a UTC timestamp")
    if doc["onboarding_state"] not in ("greeted", "answered", "skipped"):
        problems.append("identity.onboarding_state is not a known state")
    if len(problems) > before:
        return None
    return IdentityRecord(
        identity_id=str(doc["identity_id"]),
        seed_version=str(doc["seed_version"]),
        style_version=str(doc["style_version"]),
        created_at=str(doc["created_at"]),
        awakened_at=str(doc["awakened_at"]),
        onboarding_state=cast(Any, doc["onboarding_state"]),
        updated_at=str(doc["updated_at"]),
    )


def _validate_facts(
    raw_facts: object, identity: IdentityRecord | None, problems: list[str]
) -> list[MemoryFact]:
    if not isinstance(raw_facts, list):
        problems.append("memory.facts must be a list")
        return []
    if len(raw_facts) > MAX_FACTS:
        problems.append(f"memory.facts has more than {MAX_FACTS} entries")
        return []
    facts: list[MemoryFact] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_facts):
        label = f"memory.facts[{index}]"
        if not isinstance(item, dict) or not _check_keys(
            label, item, _FACT_KEYS, problems
        ):
            if not isinstance(item, dict):
                problems.append(f"{label} must be an object")
            continue
        before = len(problems)
        if not _is_uuid(item["fact_id"]):
            problems.append(f"{label}.fact_id must be a canonical lowercase UUID")
        elif item["fact_id"] in seen:
            problems.append(f"{label}.fact_id is duplicated")
        else:
            seen.add(item["fact_id"])
        if item["category"] not in CATEGORIES:
            problems.append(f"{label}.category is not a known category")
        value = item["value"]
        if not isinstance(value, str):
            problems.append(f"{label}.value must be text")
        else:
            try:
                clean = validate_fact_value(value)
            except InvalidFactError as exc:
                problems.append(f"{label}.value: {exc}")
            else:
                if clean != value:
                    problems.append(f"{label}.value has leading or trailing whitespace")
        if item["source"] != SOURCE_EXPLICIT:
            problems.append(f"{label}.source is not supported")
        if item["confirmation"] != CONFIRMATION_LOCAL:
            problems.append(f"{label}.confirmation is not supported")
        for key in ("created_at", "updated_at", "confirmed_at"):
            if not _is_timestamp(item[key]):
                problems.append(f"{label}.{key} must be a UTC timestamp")
        count = item["correction_count"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 100_000
        ):
            problems.append(f"{label}.correction_count must be a small integer")
        if item["sensitivity"] != "normal" or item["retention"] != "until_deleted":
            problems.append(f"{label}.sensitivity/retention not supported")
        if item["imported_at"] is not None and not _is_timestamp(item["imported_at"]):
            problems.append(f"{label}.imported_at must be null or a UTC timestamp")
        if item["imported_from"] is not None and not _is_uuid(item["imported_from"]):
            problems.append(f"{label}.imported_from must be null or a UUID")
        if len(problems) > before or identity is None:
            continue
        facts.append(
            MemoryFact(
                fact_id=item["fact_id"],
                identity_id=identity.identity_id,
                category=item["category"],
                value=item["value"],
                source=item["source"],
                confirmation=item["confirmation"],
                created_at=item["created_at"],
                updated_at=item["updated_at"],
                confirmed_at=item["confirmed_at"],
                correction_count=item["correction_count"],
                sensitivity=item["sensitivity"],
                retention=item["retention"],
                imported_at=item["imported_at"],
                imported_from=item["imported_from"],
            )
        )
    if sum(1 for f in facts if f.category == "preferred_name") > 1:
        problems.append("memory.facts holds more than one preferred_name")
    return facts


def _content(fact: MemoryFact) -> tuple[object, ...]:
    return (
        fact.category,
        fact.value,
        fact.source,
        fact.confirmation,
        fact.created_at,
        fact.updated_at,
        fact.confirmed_at,
        fact.correction_count,
    )


def plan_import(
    bundle: ImportBundle,
    *,
    local_identity_id: str,
    local_facts: tuple[MemoryFact, ...],
    tombstones: dict[str, str],
) -> ImportPlan:
    """Decide, without writing, what importing ``bundle`` would do locally."""
    if bundle.identity.identity_id != local_identity_id:
        return ImportPlan(
            add=(),
            unchanged=0,
            conflicts=(
                "The file belongs to a different Bunny identity than this profile. "
                "Identities are never merged or overwritten; import into a fresh "
                "--data-dir instead.",
            ),
        )
    by_id = {f.fact_id: f for f in local_facts}
    local_name = next((f for f in local_facts if f.category == "preferred_name"), None)
    add: list[MemoryFact] = []
    conflicts: list[str] = []
    unchanged = 0
    for fact in bundle.facts:
        label = f"{fact.category} {fact.fact_id[:8]}"
        local = by_id.get(fact.fact_id)
        if fact.fact_id in tombstones:
            conflicts.append(
                f"{label}: you deleted this fact locally on {tombstones[fact.fact_id]}; "
                "importing would bring it back."
            )
        elif local is not None:
            if _content(local) == _content(fact):
                unchanged += 1
            else:
                newer = (
                    "local copy is newer"
                    if local.updated_at > fact.updated_at
                    else ("file copy differs")
                )
                conflicts.append(
                    f"{label}: differs from the local fact ({newer}); local data was "
                    "not overwritten."
                )
        elif any(
            f.category == fact.category and f.value == fact.value for f in local_facts
        ):
            unchanged += 1
        elif fact.category == "preferred_name" and local_name is not None:
            conflicts.append(
                f"{label}: a different preferred name is already saved locally; "
                "local data was not overwritten."
            )
        else:
            add.append(fact)
    if len(local_facts) + len(add) > MAX_FACTS:
        conflicts.append(f"Importing would exceed the {MAX_FACTS}-fact limit.")
    return ImportPlan(add=tuple(add), unchanged=unchanged, conflicts=tuple(conflicts))


def _insert_fact(
    conn: sqlite3.Connection,
    fact: MemoryFact,
    identity_id: str,
    imported_at: str,
    export_id: str,
) -> None:
    conn.execute(
        f"INSERT INTO memory_facts ({FACT_COLUMNS}) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fact.fact_id,
            identity_id,
            fact.category,
            fact.value,
            fact.source,
            fact.confirmation,
            fact.created_at,
            fact.updated_at,
            fact.confirmed_at,
            fact.correction_count,
            fact.sensitivity,
            fact.retention,
            imported_at,
            export_id,
        ),
    )


def populate_fresh_profile(
    bundle: ImportBundle, imported_at: str
) -> Callable[[sqlite3.Connection], None]:
    """Return a callback that seeds a brand-new staging database from ``bundle``."""

    def populate(conn: sqlite3.Connection) -> None:
        for fact in bundle.facts:
            _insert_fact(
                conn, fact, bundle.identity.identity_id, imported_at, bundle.export_id
            )
        conn.execute(
            "UPDATE memory_settings SET enabled = ?, updated_at = ?",
            (1 if bundle.memory_enabled else 0, imported_at),
        )

    return populate


def apply_import(
    storage: LocalStorage,
    store: MemoryStore,
    bundle: ImportBundle,
    *,
    imported_at: str,
) -> ImportPlan:
    """Apply a validated bundle to an existing profile atomically.

    Conflicts are re-checked inside the write transaction; any conflict raises
    ``ImportConflictError`` and the transaction rolls back untouched.
    """
    with storage.transaction("apply import") as conn:
        plan = plan_import(
            bundle,
            local_identity_id=store.identity_id,
            local_facts=store.list_facts(),
            tombstones=store.tombstones(),
        )
        if plan.conflicts:
            raise ImportConflictError(
                "Import conflicts with local data.", plan.conflicts
            )
        for fact in plan.add:
            _insert_fact(conn, fact, store.identity_id, imported_at, bundle.export_id)
    return plan
