"""Command-line interface for Bunny.ai."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version

from bunny_ai.app import AwakeningSession, TurnOutcome, get_status, run_demo
from bunny_ai.budget import format_micro_usd
from bunny_ai.chat_commands import HELP_LINES, handle_command, is_memory_command
from bunny_ai.cli_portability import run_export, run_import, run_recover
from bunny_ai.config import AppConfig, load_config
from bunny_ai.errors import (
    ConfigError,
    ExportError,
    GatewayError,
    PortabilityError,
    StorageError,
)
from bunny_ai.ledger import BudgetLedger, LedgerSummary
from bunny_ai.memory import CATEGORY_LABELS
from bunny_ai.neon import SEED_VERSION, STYLE_VERSION, sanitize_terminal_text
from bunny_ai.storage import LocalStorage


class Options(argparse.Namespace):
    """Typed namespace for CLI arguments."""

    command: str | None
    budget_command: str | None
    text: str | None
    data_dir: str | None
    config: str | None
    history: bool | None
    skip_intro: bool
    message: str | None
    path: str | None
    force: bool
    backup: str | None
    list_backups: bool
    yes: bool
    per_request: int | None
    cumulative: int | None
    attempt_id: str | None
    release: bool
    settle: str | None
    reason: str | None


def _add_profile_flags(
    subparser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    subparser.add_argument(
        "--data-dir", default=default, help="Directory for local SQLite state"
    )
    subparser.add_argument(
        "--config", default=default, help="Path to TOML configuration file"
    )


def _build_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    parser = argparse.ArgumentParser(
        prog="bunny",
        description="Bunny.ai offline Awakening CLI",
    )
    parser.add_argument(
        "--version", action="version", version=f"Bunny.ai {version('bunny-ai')}"
    )
    commands = parser.add_subparsers(dest="command")

    demo = commands.add_parser("demo", help="Run one simulated offline turn")
    demo.add_argument("text", help="Text for the simulated turn")

    chat = commands.add_parser(
        "chat", help="Start or resume an offline Awakening session"
    )
    _add_profile_flags(chat)
    history_group = chat.add_mutually_exclusive_group()
    history_group.add_argument(
        "--history",
        dest="history",
        action="store_true",
        default=None,
        help="Opt in to saving local conversation history for this session",
    )
    history_group.add_argument(
        "--no-history",
        dest="history",
        action="store_false",
        default=None,
        help="Run a no-history session without saving turns to SQLite",
    )
    chat.add_argument(
        "--skip-intro",
        action="store_true",
        help="Skip the optional first-launch question",
    )
    chat.add_argument(
        "-m",
        "--message",
        help="Send one simulated message and exit cleanly",
    )

    status = commands.add_parser(
        "status", help="Inspect local Bunny.ai identity and storage status (read-only)"
    )
    _add_profile_flags(status)

    export = commands.add_parser(
        "export",
        help="Write an UNENCRYPTED versioned export of identity and saved memory",
    )
    export.add_argument("path", help="Export file to create")
    _add_profile_flags(export)
    export.add_argument(
        "--force", action="store_true", help="Replace an existing export file"
    )

    import_cmd = commands.add_parser(
        "import",
        help="Validate then import an export into a fresh or matching profile",
    )
    import_cmd.add_argument("path", help="Export file to import")
    _add_profile_flags(import_cmd)

    recover = commands.add_parser(
        "recover", help="List local backups or restore one over the profile"
    )
    _add_profile_flags(recover)
    recover.add_argument(
        "--list", dest="list_backups", action="store_true", help="List backups"
    )
    recover.add_argument("--backup", help="Backup file name or path to restore")
    recover.add_argument(
        "--yes", action="store_true", help="Skip the restore confirmation prompt"
    )

    budget = commands.add_parser(
        "budget", help="Inspect or manage gateway spending caps, holds, and blocks"
    )
    _add_profile_flags(budget)
    budget_sub = budget.add_subparsers(dest="budget_command")

    budget_set = budget_sub.add_parser(
        "set", help="Set per-request and/or cumulative caps in micro-USD"
    )
    _add_profile_flags(budget_set, suppress_defaults=True)
    budget_set.add_argument(
        "--per-request",
        type=int,
        default=None,
        help="Per-request ceiling cap in integer micro-USD",
    )
    budget_set.add_argument(
        "--cumulative",
        type=int,
        default=None,
        help="Cumulative profile cap in integer micro-USD",
    )

    budget_reconcile = budget_sub.add_parser(
        "reconcile", help="Reconcile an uncertain hold by releasing or settling"
    )
    _add_profile_flags(budget_reconcile, suppress_defaults=True)
    budget_reconcile.add_argument("attempt_id", help="Attempt ID or unambiguous prefix")
    action_group = budget_reconcile.add_mutually_exclusive_group(required=True)
    action_group.add_argument(
        "--release",
        action="store_true",
        help="Release the reserved hold as not billed",
    )
    action_group.add_argument(
        "--settle",
        nargs="?",
        const="__RESERVED__",
        default=None,
        metavar="MICRO_USD",
        help="Settle the hold (defaults to reserved amount if omitted)",
    )
    budget_reconcile.add_argument(
        "--reason", default="operator_reconciled", help="Audit reason note"
    )

    budget_clear = budget_sub.add_parser(
        "clear-review", help="Clear dispatch block after operator review"
    )
    _add_profile_flags(budget_clear, suppress_defaults=True)
    budget_clear.add_argument(
        "--reason", default="operator_reviewed", help="Audit reason note"
    )

    return parser, demo


def _history_label(session: AwakeningSession) -> str:
    if session.history_enabled:
        return "on (opt-in local SQLite)"
    if session.history_overridden:
        return "off (no-history session)"
    return "off (opt-in required)"


def _format_micro_or_unset(amount: int | None) -> str:
    return (
        format_micro_usd(amount) if amount is not None else "unset (refuses dispatch)"
    )


def _budget_label(session: AwakeningSession, config: AppConfig) -> str:
    summary = session.ledger.summary()
    avail = _format_micro_or_unset(summary.available_micro_usd)
    base = (
        f"settled {format_micro_usd(summary.settled_micro_usd)} | "
        f"held {format_micro_usd(summary.held_micro_usd)} | "
        f"available {avail} (profile: {config.gateway.profile})"
    )
    if summary.dispatch_blocked:
        reason = sanitize_terminal_text(summary.block_reason or "review required")
        return f"{base} | BLOCKED ({reason})"
    return base


def _print_banner(session: AwakeningSession, config: AppConfig) -> None:
    init = session.init_result
    launch_label = "First launch" if init.is_first_launch else "Returning"
    print(f"Identity: {init.identity.identity_id}")
    print(f"Seed: {init.identity.seed_version} ({init.identity.style_version})")
    print(f"Status: {launch_label} | Mode: offline simulated")
    print(f"History: {_history_label(session)}")
    print(f"Memory: {_memory_label(session)}")
    print(f"Budget: {_budget_label(session, config)}")
    print(f"Data dir: {config.data_dir}")
    if init.backup_path is not None:
        print(f"Preflight backup: {init.backup_path}")


def _memory_label(session: AwakeningSession) -> str:
    enabled = session.memory.settings().enabled
    count = len(session.memory.list_facts())
    if enabled:
        return f"on ({count} saved fact(s); saving always asks first)"
    return f"off ({count} saved fact(s) kept but not used)"


def _print_chat_help() -> None:
    print("Commands: /help, /status, /skip (intro), /exit, /quit")
    for line in HELP_LINES:
        print(line)


def _print_session_status(session: AwakeningSession) -> None:
    summary = session.ledger.summary()
    avail = _format_micro_or_unset(summary.available_micro_usd)
    print(
        f"Identity: {session.identity.identity_id} | "
        f"Seed: {session.identity.seed_version} | "
        f"Onboarding: {session.identity.onboarding_state} | "
        f"History: {_history_label(session)} | "
        f"Memory: {_memory_label(session)} | "
        f"Budget available: {avail}"
    )


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _format_non_settled_outcome(outcome: TurnOutcome, summary: LedgerSummary) -> str:
    detail = sanitize_terminal_text(
        outcome.reason or outcome.refusal_code or "request not completed"
    )
    balances = (
        f"settled {format_micro_usd(summary.settled_micro_usd)} | "
        f"held {format_micro_usd(summary.held_micro_usd)} | "
        f"available {_format_micro_or_unset(summary.available_micro_usd)}"
    )
    if outcome.status == "refused":
        code = sanitize_terminal_text(outcome.refusal_code or "refused")
        return f"Dispatch refused ({code}): {detail} [{balances}]"
    if outcome.status == "released":
        return (
            f"Dispatch not sent; released reservation "
            f"{format_micro_usd(outcome.reserved_micro_usd)}: {detail} [{balances}]"
        )
    if outcome.status == "uncertain_hold":
        return (
            f"Dispatch outcome uncertain; holding "
            f"{format_micro_usd(outcome.held_micro_usd)} "
            f"(attempt {outcome.attempt_id[:8]}): {detail} [{balances}]"
        )
    return (
        f"Dispatch blocked after settlement/overrun "
        f"(attempt {outcome.attempt_id[:8]}, settled {format_micro_usd(outcome.settled_micro_usd)}, "
        f"overrun {format_micro_usd(outcome.overrun_micro_usd)}): {detail} [{balances}]"
    )


def _print_reply(outcome: TurnOutcome, summary: LedgerSummary) -> None:
    if outcome.status != "settled":
        print(_format_non_settled_outcome(outcome, summary))
        return
    print(f"Bunny> {outcome.reply}")
    print(
        f"Turn spend: {format_micro_usd(outcome.settled_micro_usd)} "
        f"(reserved {format_micro_usd(outcome.reserved_micro_usd)})"
    )
    if outcome.facts_used:
        used = "; ".join(
            f"{CATEGORY_LABELS[f.category]} = {sanitize_terminal_text(f.value)}"
            for f in outcome.facts_used
        )
        print(f"Memory used this turn: {used}")


def _run_chat(args: Options) -> int:
    config = load_config(cli_config_path=args.config, cli_data_dir=args.data_dir)
    session = AwakeningSession(config, history_override=args.history)
    try:
        init = session.open()
        _print_banner(session, config)
        print(session.greeting_text(skip_intro=args.skip_intro))

        if args.message is not None:
            if init.is_first_launch and args.skip_intro:
                session.skip_onboarding()
            try:
                outcome = session.handle_turn(
                    args.message,
                    from_intro=init.is_first_launch and not args.skip_intro,
                )
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            if outcome.status != "settled":
                print(
                    _format_non_settled_outcome(outcome, session.ledger.summary()),
                    file=sys.stderr,
                )
                return 2 if outcome.status == "refused" else 1
            _print_reply(outcome, session.ledger.summary())
            print("Bunny session closed.")
            return 0

        if init.is_first_launch:
            if args.skip_intro or not config.onboarding.ask_intro_question:
                session.skip_onboarding()
            else:
                while True:
                    try:
                        intro_raw = input("You (optional)> ")
                    except (EOFError, KeyboardInterrupt):
                        print("\nBunny session closed.")
                        return 0
                    intro_cmd = intro_raw.strip()
                    if not intro_cmd or intro_cmd == "/skip":
                        session.skip_onboarding()
                        print("Skipped intro question. Ask anything or type /help.")
                        break
                    if intro_cmd in ("/exit", "/quit"):
                        print("Bunny session closed.")
                        return 0
                    if intro_cmd == "/help":
                        _print_chat_help()
                        continue
                    if intro_cmd == "/status":
                        _print_session_status(session)
                        continue
                    if is_memory_command(intro_cmd):
                        handle_command(session, intro_cmd, _confirm)
                        continue
                    try:
                        outcome = session.handle_turn(intro_raw, from_intro=True)
                    except ValueError as exc:
                        print(f"Input rejected: {sanitize_terminal_text(str(exc))}")
                        continue
                    _print_reply(outcome, session.ledger.summary())
                    break

        while True:
            try:
                raw_line = input("You> ")
            except (EOFError, KeyboardInterrupt):
                print("\nBunny session closed.")
                return 0
            command = raw_line.strip()
            if not command:
                continue
            if command in ("/exit", "/quit"):
                print("Bunny session closed.")
                return 0
            if command == "/help":
                _print_chat_help()
                continue
            if command == "/status":
                _print_session_status(session)
                continue
            if is_memory_command(command):
                handle_command(session, command, _confirm)
                continue
            if command == "/skip":
                print("Onboarding is not active.")
                continue
            try:
                outcome = session.handle_turn(raw_line)
            except ValueError as exc:
                print(f"Input rejected: {sanitize_terminal_text(str(exc))}")
                continue
            _print_reply(outcome, session.ledger.summary())
    finally:
        session.close()


def _run_status(args: Options) -> int:
    config = load_config(cli_config_path=args.config, cli_data_dir=args.data_dir)
    snapshot = get_status(config)
    print(f"Data dir: {config.data_dir}")
    print(f"Config file: {config.config_path if config.config_path else 'defaults'}")
    print(f"Database: {snapshot.db_path}")
    print(
        "Default history: "
        f"{'enabled' if config.history.enabled else 'disabled (opt-in)'}"
    )
    if not snapshot.exists or snapshot.identity is None:
        print("State: not initialized (run `bunny chat` to awaken)")
        return 0
    print(f"Schema version: {snapshot.schema_version}")
    print(f"Identity: {snapshot.identity.identity_id}")
    print(f"Seed: {snapshot.identity.seed_version} ({snapshot.identity.style_version})")
    print(f"Onboarding: {snapshot.identity.onboarding_state}")
    print(f"Awakened at: {snapshot.identity.awakened_at}")
    print(f"Saved sessions: {snapshot.session_count}")
    print(f"Saved turns: {snapshot.turn_count}")
    if snapshot.memory_enabled is not None:
        state = "on" if snapshot.memory_enabled else "off"
        print(f"Memory: {state}")
        print(f"Saved facts: {snapshot.fact_count}")
    if (
        (snapshot.schema_version is not None and snapshot.schema_version >= 4)
        or snapshot.dispatch_blocked
        or snapshot.attempt_count > 0
        or snapshot.settled_micro_usd > 0
        or snapshot.held_micro_usd > 0
    ):
        avail = _format_micro_or_unset(snapshot.available_micro_usd)
        print(f"Budget settled: {format_micro_usd(snapshot.settled_micro_usd)}")
        print(f"Budget held: {format_micro_usd(snapshot.held_micro_usd)}")
        print(f"Budget available: {avail}")
        print(
            f"Gateway attempts: {snapshot.attempt_count} "
            f"(uncertain holds: {snapshot.uncertain_hold_count})"
        )
        if snapshot.dispatch_blocked:
            reason = sanitize_terminal_text(snapshot.block_reason or "review required")
            print(f"Dispatch status: BLOCKED ({reason})")
        else:
            print("Dispatch status: ok")
    return 0


def _print_budget_summary(summary: LedgerSummary) -> None:
    per_req = _format_micro_or_unset(summary.per_request_micro_usd)
    cum = _format_micro_or_unset(summary.cumulative_micro_usd)
    avail = _format_micro_or_unset(summary.available_micro_usd)
    print(f"Identity: {summary.identity_id}")
    print(f"Per-request cap: {per_req}")
    print(f"Cumulative cap: {cum}")
    print(f"Settled: {format_micro_usd(summary.settled_micro_usd)}")
    print(f"Held: {format_micro_usd(summary.held_micro_usd)}")
    print(f"Available: {avail}")
    if summary.dispatch_blocked:
        reason = sanitize_terminal_text(summary.block_reason or "review required")
        print(f"Dispatch status: BLOCKED ({reason})")
    else:
        print("Dispatch status: ok")


def _run_budget(args: Options) -> int:
    config = load_config(cli_config_path=args.config, cli_data_dir=args.data_dir)
    storage = LocalStorage(
        config.data_dir, busy_timeout_ms=config.storage.busy_timeout_ms
    )
    snapshot = storage.inspect()
    if not snapshot.exists or snapshot.identity is None:
        print(
            "State: not initialized (run `bunny chat` first).",
            file=sys.stderr,
        )
        return 1

    init = storage.initialize_or_load(
        seed_version=SEED_VERSION, style_version=STYLE_VERSION
    )
    try:
        ledger = BudgetLedger(storage, init.identity.identity_id)
        ledger.check_and_reconcile_watermark()
        ledger.recover_stale_intents()
        sub = args.budget_command

        if sub is None:
            _print_budget_summary(ledger.summary())
            attempts = ledger.list_attempts(limit=10)
            if attempts:
                print("Recent attempts:")
                for att in attempts:
                    detail = (
                        att.refusal_code
                        or att.reason
                        or ("in-flight" if att.state == "reserved" else "")
                    )
                    suffix = f" ({sanitize_terminal_text(detail)})" if detail else ""
                    print(
                        f"  [{att.attempt_id[:8]}] {att.state} | "
                        f"reserved {format_micro_usd(att.reserved_micro_usd)} | "
                        f"settled {format_micro_usd(att.settled_micro_usd)} | "
                        f"held {format_micro_usd(att.held_micro_usd)}{suffix}"
                    )
            return 0

        if sub == "set":
            if args.per_request is None and args.cumulative is None:
                print(
                    "Specify --per-request and/or --cumulative in micro-USD.",
                    file=sys.stderr,
                )
                return 2
            updated = ledger.set_allowances(
                per_request_micro_usd=args.per_request,
                cumulative_micro_usd=args.cumulative,
            )
            _print_budget_summary(updated)
            return 0

        if sub == "reconcile":
            if args.attempt_id is None:
                print("Attempt id is required.", file=sys.stderr)
                return 2
            if args.release:
                att = ledger.reconcile_uncertain_hold(
                    args.attempt_id,
                    action="release",
                    reason=args.reason or "operator_reconciled",
                )
            else:
                settled_amt: int | None = None
                if args.settle is not None and args.settle != "__RESERVED__":
                    try:
                        settled_amt = int(args.settle)
                    except ValueError:
                        print(
                            f"Invalid --settle micro-USD integer: {args.settle}",
                            file=sys.stderr,
                        )
                        return 2
                att = ledger.reconcile_uncertain_hold(
                    args.attempt_id,
                    action="settle",
                    settled_micro_usd=settled_amt,
                    reason=args.reason or "operator_reconciled",
                )
            print(
                f"Reconciled [{att.attempt_id[:8]}] -> {att.state} "
                f"(settled {format_micro_usd(att.settled_micro_usd)}, "
                f"held {format_micro_usd(att.held_micro_usd)})."
            )
            _print_budget_summary(ledger.summary())
            return 0

        if sub == "clear-review":
            cleared = ledger.clear_dispatch_block(
                reason=args.reason or "operator_reviewed"
            )
            _print_budget_summary(cleared)
            return 0
    finally:
        storage.close()

    return 2


def _run_portability(args: Options) -> int:
    config = load_config(cli_config_path=args.config, cli_data_dir=args.data_dir)
    if args.command == "export":
        return run_export(config, str(args.path), force=args.force)
    if args.command == "import":
        return run_import(config, str(args.path))
    return run_recover(
        config, args.backup, list_only=args.list_backups, assume_yes=args.yes
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``bunny`` CLI."""
    parser, demo = _build_parser()
    args = parser.parse_args(argv, namespace=Options())

    if args.command is None:
        parser.print_help()
        parser.exit()

    if args.command == "demo":
        raw_text = args.text or ""
        text = raw_text.strip()
        if not text:
            demo.error("demo text must contain a non-whitespace character")
        print(run_demo(text))
        return 0

    try:
        if args.command == "chat":
            return _run_chat(args)
        if args.command == "status":
            return _run_status(args)
        if args.command == "budget":
            return _run_budget(args)
        if args.command in ("export", "import", "recover"):
            return _run_portability(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except GatewayError as exc:
        print(f"Gateway error: {sanitize_terminal_text(str(exc))}", file=sys.stderr)
        return 2
    except PortabilityError as exc:
        verb = "Export failed" if isinstance(exc, ExportError) else "Import refused"
        print(f"{verb}: {exc}", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {sanitize_terminal_text(problem)}", file=sys.stderr)
        if not isinstance(exc, ExportError):
            print("Nothing was changed.", file=sys.stderr)
        return 2
    except StorageError as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2
