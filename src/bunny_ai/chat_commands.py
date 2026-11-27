"""In-chat slash commands for user-controlled memory and history.

Every change to durable memory is proposed first and applied only after the
user answers yes. Nothing is inferred from conversation text.
"""

from __future__ import annotations

from collections.abc import Callable

from bunny_ai.app import AwakeningSession
from bunny_ai.budget import format_micro_usd
from bunny_ai.errors import GatewayError, StorageError
from bunny_ai.memory import (
    CATEGORY_LABELS,
    CONFIRMATION_LABELS,
    DELETION_LIMITS,
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_FACTS,
    MAX_FACT_CHARS,
    SOURCE_LABELS,
    Category,
    MemoryCommandError,
    MemoryFact,
    select_context_facts,
    validate_fact_value,
)
from bunny_ai.neon import sanitize_terminal_text

Confirm = Callable[[str], bool]

COMMAND_PREFIXES = ("/remember", "/memory", "/history", "/spend")

HELP_LINES = (
    "Memory (nothing is saved without your confirmation):",
    "  /remember name <value>        propose a preferred name",
    "  /remember preference <text>   propose a preference",
    "  /memory                       list saved facts and the memory switch",
    "  /memory show <id>             source, confirmation and timestamps",
    "  /memory edit <id> <value>     correct a fact (asks first)",
    "  /memory delete <id>           delete one fact",
    "  /memory reset                 delete ALL facts (asks first; history kept)",
    "  /memory off | on              stop or resume saving and using memory",
    "  /memory context               what the next turn would supply",
    "History (separate from memory):",
    "  /history                      stored turns and what can be reused",
    "  /history clear                delete stored transcript (asks first; facts kept)",
    "Budget & Gateway:",
    "  /spend                        show budget caps, settled spend, holds and attempts",
)

_CONTEXT_RESTART = (
    "This chat's running context was restarted and older saved history will not "
    "refill it automatically; earlier replies cannot re-supply the old value."
)


def is_memory_command(line: str) -> bool:
    """Return True when ``line`` is one of the memory/history slash commands."""
    head = line.strip().split(None, 1)
    return bool(head) and head[0] in COMMAND_PREFIXES


def _text(value: str) -> str:
    return sanitize_terminal_text(value)


def _short(fact: MemoryFact) -> str:
    return fact.fact_id[:8]


def _label(fact: MemoryFact) -> str:
    return CATEGORY_LABELS[fact.category]


def handle_command(session: AwakeningSession, line: str, confirm: Confirm) -> None:
    """Run one memory/history/spend command, printing results and refusals."""
    try:
        _dispatch(session, line.strip(), confirm)
    except MemoryCommandError as exc:
        print(f"Memory: {_text(str(exc))}")
    except GatewayError as exc:
        print(f"Budget: {_text(str(exc))}")
    except StorageError as exc:
        print(f"Storage error: {_text(str(exc))}")


def _dispatch(session: AwakeningSession, line: str, confirm: Confirm) -> None:
    parts = line.split(None, 2)
    command = parts[0]
    if command == "/remember":
        _remember(session, parts[1:], confirm)
    elif command == "/history":
        _history(session, parts[1:], confirm)
    elif command == "/spend":
        _spend(session, parts[1:])
    else:
        _memory(session, line.split(None, 3)[1:], confirm)


def _remember(session: AwakeningSession, args: list[str], confirm: Confirm) -> None:
    usage = "Usage: /remember name <value>  |  /remember preference <text>"
    if len(args) < 2 or args[0] not in ("name", "preference"):
        print(usage)
        return
    category: Category = "preferred_name" if args[0] == "name" else "preference"
    value = validate_fact_value(args[1])
    store = session.memory
    if not store.settings().enabled:
        print("Memory is off. Turn it on with /memory on before saving facts.")
        return
    current = next((f for f in store.list_facts() if f.category == category), None)
    print("Proposed memory (NOT saved yet):")
    print(f"  {CATEGORY_LABELS[category]}: {_text(value)}")
    print("  source: your /remember command in this chat (nothing was inferred)")
    if category == "preferred_name" and current is not None:
        print(f"  replaces: {_text(current.value)}")
    print(
        "  stored in plaintext on this device; "
        "in this offline build it never leaves your machine"
    )
    if not confirm("Save this to durable local memory? [y/N] "):
        print("Not saved. Nothing was stored.")
        return
    fact = store.save(category, value)
    print(
        f"Saved {_label(fact)} [{_short(fact)}]. "
        f"Inspect it with /memory show {_short(fact)}."
    )
    if category == "preferred_name" and current is not None:
        print(_CONTEXT_RESTART)
        print(
            "Earlier exports, backups and saved history may still hold the old value."
        )


def _memory(session: AwakeningSession, args: list[str], confirm: Confirm) -> None:
    store = session.memory
    sub = args[0] if args else "list"
    if sub == "list":
        _print_list(session)
    elif sub == "show" and len(args) == 2:
        _print_fact(session, store.resolve(args[1]))
    elif sub == "edit" and len(args) == 3:
        fact = store.resolve(args[1])
        new_value = validate_fact_value(args[2])
        if not store.settings().enabled:
            print("Memory is off. Turn it on with /memory on before correcting facts.")
            return
        print("Proposed correction (NOT applied yet):")
        print(
            f"  {_label(fact)} [{_short(fact)}]: {_text(fact.value)} -> {_text(new_value)}"
        )
        if not confirm("Apply this correction? [y/N] "):
            print("Not changed. The saved fact is as before.")
            return
        updated = store.correct(fact.fact_id, new_value)
        print(
            f"Corrected {_label(updated)} [{_short(updated)}]. It applies from the next turn."
        )
        print(_CONTEXT_RESTART)
        print(
            "Earlier exports, backups and saved history may still hold the old value."
        )
    elif sub == "delete" and len(args) == 2:
        fact = store.resolve(args[1])
        store.delete(fact.fact_id)
        print(
            f"Deleted {_label(fact)} [{_short(fact)}]. It is excluded from the next turn."
        )
        print(_CONTEXT_RESTART)
        print(DELETION_LIMITS)
    elif sub == "reset" and len(args) == 1:
        count = len(store.list_facts())
        if count == 0:
            print("No saved facts. Nothing to reset.")
            return
        print(
            f"This deletes all {count} saved fact(s). Chat history and the memory "
            "switch are NOT affected."
        )
        if not confirm("Delete all saved facts? [y/N] "):
            print("Not changed. All facts are still saved.")
            return
        deleted = store.reset()
        print(f"Deleted {deleted} fact(s). None will be used from the next turn.")
        print(_CONTEXT_RESTART)
        print(DELETION_LIMITS)
    elif sub == "off" and len(args) == 1:
        store.set_enabled(False)
        print(
            "Memory is OFF: no facts are supplied, saved or corrected from the next "
            "turn. Saved facts are kept; use /memory delete or /memory reset to remove them."
        )
        print(_CONTEXT_RESTART)
    elif sub == "on" and len(args) == 1:
        store.set_enabled(True)
        print(
            "Memory is ON. Saved facts apply from the next turn; saving still asks first."
        )
    elif sub == "context" and len(args) == 1:
        _print_context(session)
    else:
        print("Unknown or incomplete /memory command. Type /help for the list.")


def _print_list(session: AwakeningSession) -> None:
    store = session.memory
    enabled = store.settings().enabled
    facts = store.list_facts()
    state = "on" if enabled else "OFF (facts kept but not used)"
    print(f"Memory: {state} | {len(facts)} saved fact(s)")
    for fact in facts:
        print(f"  [{_short(fact)}] {_label(fact)}: {_text(fact.value)}")
    if not facts:
        print("  (none) Propose one with /remember name <value>.")


def _print_fact(session: AwakeningSession, fact: MemoryFact) -> None:
    store = session.memory
    enabled = store.settings().enabled
    selection = select_context_facts(store.list_facts())
    in_context = enabled and any(c.fact_id == fact.fact_id for c in selection.facts)
    print(f"[{_short(fact)}] {_label(fact)}: {_text(fact.value)}")
    print(f"  id: {fact.fact_id}")
    print(f"  source: {SOURCE_LABELS.get(fact.source, fact.source)}")
    print(
        f"  confirmation: {CONFIRMATION_LABELS.get(fact.confirmation, fact.confirmation)}"
    )
    print(f"  confirmed at: {fact.confirmed_at}")
    print(f"  created: {fact.created_at} | last changed: {fact.updated_at}")
    print(f"  corrections: {fact.correction_count}")
    print("  explicit: yes (never inferred) | retention: until you delete it")
    if fact.imported_at is not None:
        print(f"  imported: {fact.imported_at} from export {fact.imported_from}")
    print(f"  supplied to the next turn: {'yes' if in_context else 'no'}")


def _print_context(session: AwakeningSession) -> None:
    store = session.memory
    settings = store.settings()
    if not settings.enabled:
        print("Memory is OFF: the next turn supplies 0 facts.")
        return
    selection = select_context_facts(store.list_facts())
    print(
        f"The next turn supplies {len(selection.facts)} fact(s) "
        f"(cap {MAX_CONTEXT_FACTS} facts / {MAX_CONTEXT_CHARS} chars, each at most "
        f"{MAX_FACT_CHARS}); {selection.omitted} left out."
    )
    for fact in selection.facts:
        print(
            f"  [{fact.fact_id[:8]}] {CATEGORY_LABELS[fact.category]}: {_text(fact.value)}"
        )


def _history(session: AwakeningSession, args: list[str], confirm: Confirm) -> None:
    stored, eligible = session.history_counts()
    mode = "on" if session.history_enabled else "off for this session"
    if args == ["clear"]:
        if stored == 0:
            print("No stored history. Nothing to clear.")
            return
        print(
            f"This deletes {stored} stored turn(s). Saved memory facts and the memory "
            "switch are NOT affected."
        )
        if not confirm("Delete stored chat history? [y/N] "):
            print("Not changed. History is still stored.")
            return
        turns, _ = session.clear_history()
        print(f"Deleted {turns} stored turn(s). Saved memory facts are unchanged.")
        print(
            "Earlier exports exclude history; backups taken earlier may still hold it."
        )
        return
    if args:
        print("Usage: /history  |  /history clear")
        return
    print(
        f"History recording: {mode} | {stored} stored turn(s), {eligible} eligible "
        "for automatic reuse in a new chat."
    )
    print(
        "Clearing history never deletes memory facts; /memory reset never deletes history."
    )


def _spend(session: AwakeningSession, args: list[str]) -> None:
    if args:
        print("Usage: /spend")
        return
    summary = session.ledger.summary()
    per_req = (
        format_micro_usd(summary.per_request_micro_usd)
        if summary.per_request_micro_usd is not None
        else "unset (refuses dispatch)"
    )
    cum = (
        format_micro_usd(summary.cumulative_micro_usd)
        if summary.cumulative_micro_usd is not None
        else "unset (refuses dispatch)"
    )
    avail = (
        format_micro_usd(summary.available_micro_usd)
        if summary.available_micro_usd is not None
        else "unset (refuses dispatch)"
    )
    print(
        f"Budget: settled {format_micro_usd(summary.settled_micro_usd)} | "
        f"held {format_micro_usd(summary.held_micro_usd)} | "
        f"available {avail} | caps: per-request {per_req}, cumulative {cum}"
    )
    if summary.dispatch_blocked:
        print(
            f"Dispatch: BLOCKED ({_text(summary.block_reason or 'review required')}); "
            "run `bunny budget clear-review` after reconciling."
        )
    else:
        print("Dispatch: ok")
    attempts = session.ledger.list_attempts(limit=5)
    if not attempts:
        print("Recent attempts: (none)")
        return
    print("Recent attempts:")
    for att in attempts:
        detail = att.refusal_code or att.reason or ""
        suffix = f" ({_text(detail)})" if detail else ""
        print(
            f"  [{att.attempt_id[:8]}] {att.state} | "
            f"reserved {format_micro_usd(att.reserved_micro_usd)} | "
            f"settled {format_micro_usd(att.settled_micro_usd)} | "
            f"held {format_micro_usd(att.held_micro_usd)}{suffix}"
        )
