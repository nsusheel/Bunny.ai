"""Installed-CLI tests for user-controlled memory (Awakening Stage 3, part 1).

Every profile is a disposable temp directory and every name is synthetic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from chat_driver import ChatDriver, short_ids
from test_cli_integration import _cli_env, _extract_identity, _run_cli

from bunny_ai.storage import DB_FILENAME

NAME = "Zorblax"
PREF = "Prefers answers in haiku form"


def _facts(data_dir: Path) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        return conn.execute(
            "SELECT category, value, source, confirmation FROM memory_facts "
            "ORDER BY category"
        ).fetchall()
    finally:
        conn.close()


def _start(tmp_path: Path, data_dir: Path, *extra: str) -> ChatDriver:
    return ChatDriver(
        ["--data-dir", str(data_dir), *extra], _cli_env(tmp_path), tmp_path
    )


def test_decline_confirm_restart_edit_delete(tmp_path: Path) -> None:
    data_dir = tmp_path / "profile"
    with _start(tmp_path, data_dir) as chat:
        assert "Memory: on (0 saved fact(s); saving always asks first)" in chat.opening
        chat.send("/skip")

        # Declining stores nothing and says so.
        prompt = chat.send(f"/remember name {NAME}")
        assert "Proposed memory (NOT saved yet):" in prompt
        assert f"preferred name: {NAME}" in prompt
        assert "source: your /remember command" in prompt
        assert "Save this to durable local memory? [y/N]" in prompt
        declined = chat.send("n")
        assert "Not saved. Nothing was stored." in declined
        assert _facts(data_dir) == []
        assert "0 confirmed memory fact(s) supplied" in chat.send("hello")

        # EOF-style blank answer also declines.
        chat.send(f"/remember preference {PREF}")
        assert "Not saved." in chat.send("")
        assert _facts(data_dir) == []

        # Confirming saves exactly the shown value with provenance.
        chat.send(f"/remember name {NAME}")
        saved = chat.send("y")
        assert "Saved preferred name" in saved
        chat.send(f"/remember preference {PREF}")
        chat.send("yes")
        assert _facts(data_dir) == [
            ("preference", PREF, "explicit_command", "user_confirmed_local"),
            ("preferred_name", NAME, "explicit_command", "user_confirmed_local"),
        ]
        reply = chat.send("hello again")
        assert "2 confirmed memory fact(s) supplied" in reply
        assert f"preferred name = {NAME}" in reply
        assert chat.finish() == 0

    # Restart: facts are reused without any new prompt.
    with _start(tmp_path, data_dir) as chat:
        assert "Memory: on (2 saved fact(s)" in chat.opening
        listing = chat.send("/memory")
        ids = short_ids(listing)
        assert len(ids) == 2
        name_id = ids[0]
        shown = chat.send(f"/memory show {name_id}")
        assert "source: you typed it with a /remember or /memory command" in shown
        assert "confirmation: you confirmed it locally before it was saved" in shown
        assert "confirmed at: " in shown
        assert "supplied to the next turn: yes" in shown

        # Correction asks first and takes effect on the very next turn.
        proposal = chat.send(f"/memory edit {name_id} Quillon")
        assert f"{NAME} -> Quillon" in proposal
        assert "Not changed" in chat.send("n")
        assert f"preferred name = {NAME}" in chat.send("still old?")
        chat.send(f"/memory edit {name_id} Quillon")
        corrected = chat.send("y")
        assert "applies from the next turn" in corrected
        assert "Earlier exports, backups and saved history may still hold" in corrected
        after_edit = chat.send("now?")
        assert "preferred name = Quillon" in after_edit
        assert NAME not in after_edit

        # Deletion also applies on the next turn, in the already-open chat.
        deleted = chat.send(f"/memory delete {name_id}")
        assert "excluded from the next turn" in deleted
        assert "earlier exports, backups" in deleted
        after_delete = chat.send("and now?")
        assert "1 confirmed memory fact(s) supplied" in after_delete
        assert "Quillon" not in after_delete
        assert chat.finish() == 0
    assert [row[0] for row in _facts(data_dir)] == ["preference"]


def test_disabled_memory_blocks_use_and_saving_across_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "profile"
    with _start(tmp_path, data_dir) as chat:
        chat.send("/skip")
        chat.send(f"/remember name {NAME}")
        chat.send("y")
        assert "1 confirmed memory fact(s)" in chat.send("one")
        off = chat.send("/memory off")
        assert "Memory is OFF" in off
        assert "saved facts are kept" in off.lower()
        assert "0 confirmed memory fact(s)" in chat.send("two")
        assert "Memory used this turn" not in chat.send("three")
        refused = chat.send(f"/remember preference {PREF}")
        assert "Memory is off" in refused
        assert "Save this to durable" not in refused
        assert "supplies 0 facts" in chat.send("/memory context")
        assert chat.finish() == 0

    status = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=_cli_env(tmp_path), cwd=tmp_path
    )
    assert "Memory: off" in status.stdout
    assert "Saved facts: 1" in status.stdout

    with _start(tmp_path, data_dir) as chat:
        assert "Memory: off (1 saved fact(s) kept but not used)" in chat.opening
        assert "0 confirmed memory fact(s)" in chat.send("after restart")
        assert "Memory is ON" in chat.send("/memory on")
        assert f"preferred name = {NAME}" in chat.send("back on")
        chat.finish()


def test_history_and_memory_resets_are_independent_and_explained(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "profile"
    with _start(tmp_path, data_dir, "--history") as chat:
        chat.send("/skip")
        chat.send("synthetic turn one")
        chat.send("synthetic turn two")
        chat.send(f"/remember name {NAME}")
        chat.send("y")

        clear = chat.send("/history clear")
        assert "Saved memory facts and the memory switch are NOT affected" in clear
        assert "Delete stored chat history? [y/N]" in clear
        cleared = chat.send("y")
        assert "Deleted 2 stored turn(s). Saved memory facts are unchanged." in cleared
        assert "0 stored turn(s)" in chat.send("/history")
        assert f"preferred name = {NAME}" in chat.send("facts survive history reset")
        chat.finish()
    assert len(_facts(data_dir)) == 1

    with _start(tmp_path, data_dir, "--history") as chat:
        assert "1 prior turn(s) loaded" in chat.opening
        reset = chat.send("/memory reset")
        assert "Chat history and the memory switch are NOT affected" in reset
        assert "Not changed" in chat.send("n")
        assert len(_facts(data_dir)) == 1
        chat.send("/memory reset")
        done = chat.send("y")
        assert "Deleted 1 fact(s)" in done
        assert "earlier exports, backups" in done
        assert "0 confirmed memory fact(s)" in chat.send("after memory reset")
        chat.finish()
    assert _facts(data_dir) == []

    status = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=_cli_env(tmp_path), cwd=tmp_path
    )
    assert "Saved turns: 2" in status.stdout  # history survived the memory reset


def test_fact_change_stops_old_history_refilling_new_chats(tmp_path: Path) -> None:
    data_dir = tmp_path / "profile"
    with _start(tmp_path, data_dir, "--history") as chat:
        chat.send("/skip")
        chat.send("older synthetic turn")
        chat.send(f"/remember name {NAME}")
        chat.send("y")
        deleted_ref = short_ids(chat.send("/memory"))[0]
        chat.send(f"/memory delete {deleted_ref}")
        chat.finish()
    with _start(tmp_path, data_dir, "--history") as chat:
        assert "0 prior turn(s) loaded" in chat.opening
        history = chat.send("/history")
        assert (
            "2 stored turn(s), 0 eligible" in history
            or "1 stored turn(s), 0 eligible" in history
        )
        chat.finish()


def test_revocation_in_one_process_reaches_an_open_chat_in_another(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "profile"
    env = _cli_env(tmp_path)
    with _start(tmp_path, data_dir) as setup:
        setup.send("/skip")
        setup.send(f"/remember name {NAME}")
        setup.send("y")
        name_id = short_ids(setup.send("/memory"))[0]
        setup.finish()

    with _start(tmp_path, data_dir) as open_chat:
        assert f"preferred name = {NAME}" in open_chat.send("before")
        other = _run_cli(
            ["chat", "--data-dir", str(data_dir)],
            env=env,
            cwd=tmp_path,
            stdin_text=f"/memory delete {name_id}\n/exit\n",
        )
        assert other.returncode == 0, other.stderr
        assert "Deleted preferred name" in other.stdout
        after = open_chat.send("after")
        assert "0 confirmed memory fact(s) supplied" in after
        assert NAME not in after
        open_chat.finish()


def test_memory_refusals_leave_state_untouched(tmp_path: Path) -> None:
    data_dir = tmp_path / "profile"
    with _start(tmp_path, data_dir) as chat:
        chat.send("/skip")
        assert "Usage: /remember" in chat.send("/remember")
        assert "limited to 200 characters" in chat.send("/remember name " + "x" * 201)
        assert "No saved fact matches" in chat.send("/memory show deadbeef")
        assert "at least 4 characters" in chat.send("/memory delete ab")
        chat.send(f"/remember preference {PREF}")
        chat.send("y")
        chat.send(f"/remember preference {PREF}")
        assert "already saved" in chat.send("y")
        chat.finish()
    assert len(_facts(data_dir)) == 1


def test_identity_is_unchanged_by_memory_operations(tmp_path: Path) -> None:
    data_dir = tmp_path / "profile"
    env = _cli_env(tmp_path)
    first = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--skip-intro", "-m", "hi"],
        env=env,
        cwd=tmp_path,
    )
    identity = _extract_identity(first.stdout)
    with _start(tmp_path, data_dir) as chat:
        chat.send(f"/remember name {NAME}")
        chat.send("y")
        chat.finish()
    again = _run_cli(["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path)
    assert _extract_identity(again.stdout) == identity


def test_preferred_name_recall_across_restart_edit_disable_delete_without_transcript_copy(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "recall-profile"
    with _start(tmp_path, data_dir, "--history") as chat:
        chat.send("/skip")

        # Before saving and after declining: no recall.
        assert "I don't have a confirmed preferred name for you right now" in chat.send(
            "What is my preferred name?"
        )
        chat.send(f"/remember name {NAME}")
        assert "Not saved" in chat.send("n")
        assert "I don't have a confirmed preferred name for you right now" in chat.send(
            "What is my preferred name?"
        )

        # After confirming: visible recall in Bunny> reply.
        chat.send(f"/remember name {NAME}")
        chat.send("y")
        recalled = chat.send("What is my preferred name?")
        assert (
            f"Bunny> [Offline simulated | seed-1] Your confirmed preferred name is {NAME}"
            in recalled
        )
        assert f"Memory used this turn: preferred name = {NAME}" in recalled
        assert chat.finish() == 0

    # After restart: visible recall persists without re-prompting.
    with _start(tmp_path, data_dir, "--history") as chat:
        restarted = chat.send("What's my name?")
        assert f"Your confirmed preferred name is {NAME}" in restarted
        name_id = short_ids(chat.send("/memory"))[0]

        # After correction in same open chat: immediately uses corrected name.
        chat.send(f"/memory edit {name_id} Quillon")
        chat.send("y")
        corrected = chat.send("What should you call me?")
        assert "Your confirmed preferred name is Quillon" in corrected
        assert NAME not in corrected

        # After disablement and re-enablement:
        chat.send("/memory off")
        disabled = chat.send("Who am I?")
        assert "I don't have a confirmed preferred name for you right now" in disabled
        assert "Quillon" not in disabled
        assert "Memory used this turn" not in disabled
        chat.send("/memory on")
        restored = chat.send("Do you know my preferred name?")
        assert "Your confirmed preferred name is Quillon" in restored

        # After deletion in same open chat:
        chat.send(f"/memory delete {name_id}")
        deleted = chat.send("What is my preferred name?")
        assert "I don't have a confirmed preferred name for you right now" in deleted
        assert "Quillon" not in deleted
        assert chat.finish() == 0

    # After another restart post-deletion:
    with _start(tmp_path, data_dir, "--history") as chat:
        after_restart = chat.send("What is my preferred name?")
        assert (
            "I don't have a confirmed preferred name for you right now" in after_restart
        )
        assert "Quillon" not in after_restart
        assert NAME not in after_restart
        chat.finish()

    # Verify saved transcripts never persisted a copy of either synthetic name.
    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        assistant_texts = conn.execute("SELECT assistant_text FROM turns").fetchall()
        assert len(assistant_texts) == 9
        assert all(
            NAME not in text and "Quillon" not in text for (text,) in assistant_texts
        )
        assert conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (0,)
        tombstones = conn.execute(
            "SELECT fact_id, deleted_at FROM memory_tombstones"
        ).fetchall()
        assert len(tombstones) == 1
        assert all(
            NAME not in str(cell) and "Quillon" not in str(cell)
            for row in tombstones
            for cell in row
        )
    finally:
        conn.close()
