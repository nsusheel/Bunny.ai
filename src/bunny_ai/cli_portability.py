"""Command handlers for export, import and recovery of a local profile."""

from __future__ import annotations

import sys
from importlib.metadata import version
from pathlib import Path

from bunny_ai.config import AppConfig
from bunny_ai.errors import (
    ImportConflictError,
    ImportValidationError,
    StorageError,
)
from bunny_ai.memory import DELETION_LIMITS, MemoryStore, utc_now_iso
from bunny_ai.neon import SEED_VERSION, STYLE_VERSION, sanitize_terminal_text
from bunny_ai.portability import (
    DELETION_NOTICE,
    MAX_IMPORT_BYTES,
    UNENCRYPTED_NOTICE,
    apply_import,
    build_export,
    parse_import,
    plan_import,
    populate_fresh_profile,
    validate_export_destination,
    write_export_file,
)
from bunny_ai.storage import LocalStorage, inspect_file


def _storage(config: AppConfig) -> LocalStorage:
    return LocalStorage(config.data_dir, busy_timeout_ms=config.storage.busy_timeout_ms)


def run_export(config: AppConfig, path_text: str, *, force: bool) -> int:
    """Write an unencrypted, versioned export of identity and consented memory."""
    target = validate_export_destination(
        Path(path_text),
        data_dir=config.data_dir,
        config_path=config.config_path,
        force=force,
    )
    storage = _storage(config)
    snapshot = storage.inspect()
    if not snapshot.exists:
        print(
            "State: not initialized; there is nothing to export "
            "(run `bunny chat` first).",
            file=sys.stderr,
        )
        return 1
    result = storage.initialize_or_load(
        seed_version=SEED_VERSION, style_version=STYLE_VERSION
    )
    try:
        if result.backup_path is not None:
            print(
                "Profile upgraded to the current schema before export. "
                f"Preflight backup: {result.backup_path}"
            )
        store = MemoryStore(storage, result.identity.identity_id)
        facts = store.list_facts()
        enabled = store.settings().enabled
        payload = build_export(
            result.identity,
            memory_enabled=enabled,
            facts=facts,
            bunny_version=version("bunny-ai"),
            exported_at=utc_now_iso(),
        )
        write_export_file(target, payload, force=force)
    finally:
        storage.close()
    print(f"Exported identity {result.identity.identity_id} and {len(facts)} fact(s).")
    print(f"File: {target}")
    print(f"Memory switch recorded: {'on' if enabled else 'off'}")
    print("Not included: chat history, backups, configuration.")
    print(f"WARNING: {UNENCRYPTED_NOTICE}")
    print(f"NOTE: {DELETION_NOTICE}")
    return 0


def _read_import(path_text: str) -> bytes:
    path = Path(path_text).expanduser()
    try:
        if not path.is_file():
            raise ImportValidationError(f"Import file not found: {path}")
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise ImportValidationError(
                f"Import file is larger than {MAX_IMPORT_BYTES} bytes."
            )
        return path.read_bytes()
    except OSError as exc:
        raise ImportValidationError(f"Cannot read import file '{path}': {exc}") from exc


def run_import(config: AppConfig, path_text: str) -> int:
    """Validate an export completely, then apply it without silent overwrites."""
    bundle = parse_import(_read_import(path_text))
    storage = _storage(config)
    snapshot = storage.inspect()
    imported_at = utc_now_iso()
    identity_id = bundle.identity.identity_id
    print(
        f"Import file validated: {len(bundle.facts)} fact(s), identity {identity_id}."
    )

    if not snapshot.exists or snapshot.identity is None:
        storage.initialize_imported(
            bundle.identity, populate_fresh_profile(bundle, imported_at)
        )
        storage.close()
        print(
            "Created a new profile from the import; identity and provenance preserved."
        )
        print(
            f"Memory switch: {'on' if bundle.memory_enabled else 'off'} (as exported)"
        )
        print(f"Data dir: {config.data_dir}")
        print(
            f"WARNING: {UNENCRYPTED_NOTICE.replace('This export', 'The import file')}"
        )
        return 0

    precheck = plan_import(
        bundle,
        local_identity_id=snapshot.identity.identity_id,
        local_facts=(),
        tombstones={},
    )
    if precheck.conflicts:
        raise ImportConflictError(
            "Import conflicts with local data.", precheck.conflicts
        )

    result = storage.initialize_or_load(
        seed_version=SEED_VERSION, style_version=STYLE_VERSION
    )
    try:
        if result.backup_path is not None:
            print(
                f"Profile upgraded before import. Preflight backup: {result.backup_path}"
            )
        store = MemoryStore(storage, result.identity.identity_id)
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
        if not plan.add:
            print(
                f"Nothing to import: {plan.unchanged} fact(s) already present. No changes."
            )
            return 0
        backup = storage.create_backup("pre-import")
        applied = apply_import(storage, store, bundle, imported_at=imported_at)
    finally:
        storage.close()
    print(
        f"Imported {len(applied.add)} new fact(s); {applied.unchanged} already present."
    )
    print("Local identity and memory switch were not changed.")
    print(f"Backup taken first: {backup}")
    print(f"Undo with: bunny recover --backup {backup.name} --yes")
    print(f"WARNING: {UNENCRYPTED_NOTICE.replace('This export', 'The import file')}")
    return 0


def run_recover(
    config: AppConfig, backup_ref: str | None, *, list_only: bool, assume_yes: bool
) -> int:
    """List local backups or restore one over the profile after confirmation."""
    storage = _storage(config)
    if list_only or backup_ref is None:
        infos = storage.list_backups()
        print(f"Backups in {config.data_dir / 'backups'}:")
        if not infos:
            print("  (none)")
        for info in infos:
            if info.problem is not None:
                print(
                    f"  {info.path.name}  UNUSABLE: {sanitize_terminal_text(info.problem)}"
                )
                continue
            print(
                f"  {info.path.name}  schema v{info.schema_version}  "
                f"{info.turn_count} turn(s)  {info.fact_count} fact(s)"
            )
        print("Restore one with: bunny recover --backup <name> [--yes]")
        print(
            "Backups are unencrypted and may hold facts or history you have since deleted."
        )
        return 0

    backup = Path(backup_ref).expanduser()
    if not backup.is_absolute() and not backup.exists():
        backup = config.data_dir / "backups" / backup_ref
    if not backup.is_file():
        print(f"Backup not found: {backup}", file=sys.stderr)
        return 2
    snap = inspect_file(backup, config.storage.busy_timeout_ms)
    print(
        f"Backup: {backup.name} | schema v{snap.schema_version} | "
        f"{snap.turn_count} turn(s) | {snap.fact_count} fact(s)"
    )
    print(
        "Restoring replaces the live profile database with this backup. Anything "
        "written after the backup was taken disappears from the live profile; the "
        "current database is first saved in the backups folder."
    )
    if not assume_yes:
        try:
            answer = input("Restore this backup? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Recovery cancelled. Nothing was changed.")
            return 0
    outcome = storage.restore_from_backup(backup)
    print(f"Restored identity {outcome.identity.identity_id} from {backup.name}.")
    if outcome.safety_copy is not None:
        print(f"Previous database preserved at: {outcome.safety_copy}")
    print(DELETION_LIMITS)
    return 0


__all__ = ["StorageError", "run_export", "run_import", "run_recover"]
