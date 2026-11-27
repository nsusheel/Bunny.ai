"""Command-line interface for Bunny.ai."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version

from bunny_ai.app import AwakeningSession, TurnOutcome, get_status, run_demo
from bunny_ai.chat_commands import HELP_LINES, handle_command, is_memory_command
from bunny_ai.cli_portability import run_export, run_import, run_recover
from bunny_ai.config import AppConfig, load_config
from bunny_ai.errors import ConfigError, ExportError, PortabilityError, StorageError
from bunny_ai.memory import CATEGORY_LABELS
from bunny_ai.neon import sanitize_terminal_text


class Options(argparse.Namespace):
    """Typed namespace for CLI arguments."""

    command: str | None
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
    chat.add_argument("--data-dir", help="Directory for local SQLite state")
    chat.add_argument("--config", help="Path to TOML configuration file")
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
    status.add_argument("--data-dir", help="Directory for local SQLite state")
    status.add_argument("--config", help="Path to TOML configuration file")

    export = commands.add_parser(
        "export",
        help="Write an UNENCRYPTED versioned export of identity and saved memory",
    )
    export.add_argument("path", help="Export file to create")
    export.add_argument("--data-dir", help="Directory for local SQLite state")
    export.add_argument("--config", help="Path to TOML configuration file")
    export.add_argument(
        "--force", action="store_true", help="Replace an existing export file"
    )

    import_cmd = commands.add_parser(
        "import",
        help="Validate then import an export into a fresh or matching profile",
    )
    import_cmd.add_argument("path", help="Export file to import")
    import_cmd.add_argument("--data-dir", help="Directory for local SQLite state")
    import_cmd.add_argument("--config", help="Path to TOML configuration file")

    recover = commands.add_parser(
        "recover", help="List local backups or restore one over the profile"
    )
    recover.add_argument("--data-dir", help="Directory for local SQLite state")
    recover.add_argument("--config", help="Path to TOML configuration file")
    recover.add_argument(
        "--list", dest="list_backups", action="store_true", help="List backups"
    )
    recover.add_argument("--backup", help="Backup file name or path to restore")
    recover.add_argument(
        "--yes", action="store_true", help="Skip the restore confirmation prompt"
    )

    return parser, demo


def _history_label(session: AwakeningSession) -> str:
    if session.history_enabled:
        return "on (opt-in local SQLite)"
    if session.history_overridden:
        return "off (no-history session)"
    return "off (opt-in required)"


def _print_banner(session: AwakeningSession, config: AppConfig) -> None:
    init = session.init_result
    launch_label = "First launch" if init.is_first_launch else "Returning"
    print(f"Identity: {init.identity.identity_id}")
    print(f"Seed: {init.identity.seed_version} ({init.identity.style_version})")
    print(f"Status: {launch_label} | Mode: offline simulated")
    print(f"History: {_history_label(session)}")
    print(f"Memory: {_memory_label(session)}")
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
    print(
        f"Identity: {session.identity.identity_id} | "
        f"Seed: {session.identity.seed_version} | "
        f"Onboarding: {session.identity.onboarding_state} | "
        f"History: {_history_label(session)} | "
        f"Memory: {_memory_label(session)}"
    )


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _print_reply(outcome: TurnOutcome) -> None:
    print(f"Bunny> {outcome.reply}")
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
            _print_reply(outcome)
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
                    _print_reply(outcome)
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
            _print_reply(outcome)
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
    return 0


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
        if args.command in ("export", "import", "recover"):
            return _run_portability(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
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
