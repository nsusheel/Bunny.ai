# Portability and recovery

> **Status:** implemented in the offline CLI scaffold (Awakening Stage 3). Exports are
> **unencrypted**; encryption, hosted providers, embeddings and inferred memory are not
> part of this format. The format may still change before a release.

`bunny export`, `bunny import` and `bunny recover` move identity and consented memory
between local profiles and recover from a bad import. They are deliberate user actions;
nothing is exported, imported or restored automatically.

## What an export contains

Format `bunny-ai.portable`, **format version 1**, UTF-8 JSON, written by
`bunny export PATH` (add `--force` to replace an existing file):

| Field | Meaning |
|---|---|
| `format`, `format_version` | Always `bunny-ai.portable` and `1`. A larger version is refused as unsupported. |
| `export_id`, `exported_at`, `bunny_ai_version` | Identify this export. |
| `encryption` | Always `"none"` in version 1. |
| `notice` | The unencrypted and deletion warnings, so they travel with the file. |
| `identity` | `identity_id`, `seed_version`, `style_version`, `created_at`, `awakened_at`, `onboarding_state`, `updated_at`. |
| `memory.enabled` | The memory switch, so a disabled profile stays disabled after import. |
| `memory.facts[]` | `fact_id`, `category` (`preferred_name` or `preference`), `value`, `source`, `confirmation`, `created_at`, `updated_at`, `confirmed_at`, `correction_count`, `sensitivity`, `retention`, `imported_at`, `imported_from`. |
| `integrity.sha256` | SHA-256 over the canonical `identity` and `memory` objects. It detects accidental corruption and hand edits. It is not a signature and does not authenticate the file. |

Not exported: chat history, backups, deletion tombstones, configuration and anything else
in the data directory. Provenance (`source`, `confirmation`, timestamps, correction
count) is preserved exactly; importing adds `imported_at` and `imported_from` (the export
id) without discarding the original provenance.

## Warnings the CLI shows

- Exports are **not encrypted**. Anyone who can read the file can read the identity id
  and every saved fact. The file is created with owner-only permissions on POSIX; on
  Windows the file inherits folder permissions, so store it somewhere you trust.
- Deleting a fact locally leaves it in earlier exports and in backups. Delete or protect
  those copies yourself.

## Import rules

`bunny import PATH [--data-dir DIR]` validates the entire file before it opens or changes
any profile. It checks size, JSON, format, version, exact field sets, types, lengths,
UUIDs, timestamps, enumerations, duplicate ids, the fact cap and the checksum, and it
reports every problem found. A malformed, unsupported or conflicting import exits with
status 2 and the message "Nothing was changed."

- **Fresh profile (no database):** a new profile is created atomically with the imported
  identity, facts and memory switch. The identity id is preserved.
- **Existing profile, same identity:** facts are added, never overwritten. An older
  Stage 2 profile is upgraded first with its usual preflight backup. A verified
  `bunny-pre-import-*` backup is taken before the write, and conflicts are checked again
  inside the write transaction.
- **Refused as a whole, with no partial apply:**
  - a different identity (identities are never merged or overwritten);
  - a fact with the same id but different content (local newer or file differs);
  - a fact you deleted locally (the profile keeps an id-only tombstone so an older
    export cannot resurrect it);
  - a different preferred name than the one saved locally;
  - exceeding the 20-fact limit.
- Facts that are already present are skipped. Importing the same file twice is a no-op
  with no backup.
- The local memory switch is never changed when importing into an existing profile.

## Recovery

`bunny recover --list` lists the profile's `backups/` folder (schema version, turns,
facts, or why a file is unusable). `bunny recover --backup NAME [--yes]` replaces the live
database with a validated backup:

- It asks for confirmation unless `--yes` is given.
- A backup that belongs to a different identity is refused.
- The current database is first preserved in `backups/` as `bunny-pre-recover-*`
  (`.sqlite3`, or `.unverified` when it is corrupt or mid-write).
- Anything written after the backup was taken is gone from the live profile; it survives
  only in that safety copy.

To undo an import, restore the `bunny-pre-import-*` backup that `bunny import` printed.
Backups are unencrypted copies of the whole database, including history and any facts
that existed then, so deleting a fact does not clean them. Backups are not pruned
automatically in this scaffold.
