"""In-process tests for bounded context and next-turn effect of memory changes."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from bunny_ai.app import AwakeningSession
from bunny_ai.config import AppConfig
from bunny_ai.memory import (
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_FACTS,
    MAX_FACTS,
    FactLimitError,
    InvalidFactError,
    MemoryDisabledError,
    MemoryFact,
    MemoryStore,
)
from bunny_ai.providers import FakeProvider, ProviderRequest, ProviderResponse


class RecordingProvider:
    """Records exactly what a provider would be handed on each turn."""

    mode = "recording"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            text="ok",
            mode=self.mode,
            history_turns_used=0,
            facts_used=len(request.facts),
        )


def _session(tmp_path: Path) -> tuple[AwakeningSession, RecordingProvider]:
    provider = RecordingProvider()
    session = AwakeningSession(
        AppConfig(data_dir=tmp_path / "data", config_path=None), provider=provider
    )
    session.open()
    return session, provider


def test_facts_supplied_to_provider_are_bounded_and_ordered(tmp_path: Path) -> None:
    session, provider = _session(tmp_path)
    try:
        session.memory.save("preferred_name", "Zorblax")
        for index in range(10):
            session.memory.save(
                "preference", f"synthetic preference {index} " + "p" * 150
            )
        session.handle_turn("hi")
        facts = provider.requests[-1].facts
        assert len(facts) <= MAX_CONTEXT_FACTS
        assert sum(len(f.value) for f in facts) <= MAX_CONTEXT_CHARS
        assert facts[0].category == "preferred_name"
        assert all(f.category in ("preferred_name", "preference") for f in facts)
    finally:
        session.close()


def test_change_applies_to_next_request_in_same_open_session(tmp_path: Path) -> None:
    session, provider = _session(tmp_path)
    try:
        name = session.memory.save("preferred_name", "Zorblax")
        session.handle_turn("one")
        assert [f.value for f in provider.requests[-1].facts] == ["Zorblax"]

        session.memory.correct(name.fact_id, "Quillon")
        session.handle_turn("two")
        assert [f.value for f in provider.requests[-1].facts] == ["Quillon"]

        session.memory.set_enabled(False)
        session.handle_turn("three")
        assert provider.requests[-1].facts == ()
        with pytest.raises(MemoryDisabledError):
            session.memory.save("preference", "nope")

        session.memory.set_enabled(True)
        session.memory.delete(name.fact_id)
        session.handle_turn("four")
        assert provider.requests[-1].facts == ()
        assert session.memory.list_facts() == ()
    finally:
        session.close()


def test_change_restarts_open_conversation_window(tmp_path: Path) -> None:
    session, provider = _session(tmp_path)
    try:
        name = session.memory.save("preferred_name", "Zorblax")
        session.handle_turn("one")
        session.handle_turn("two")
        assert len(provider.requests[-1].history) == 2
        session.memory.delete(name.fact_id)
        session.handle_turn("three")
        assert not provider.requests[-1].history
    finally:
        session.close()


def test_validation_and_limits(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)
    try:
        for bad in ("", "   ", "a\nb", "x" * 201, "bell\x07"):
            with pytest.raises(InvalidFactError):
                session.memory.save("preference", bad)
        for index in range(MAX_FACTS):
            session.memory.save("preference", f"fact {index}")
        with pytest.raises(FactLimitError):
            session.memory.save("preference", "one too many")
    finally:
        session.close()


def test_deleted_fact_leaves_only_an_id_tombstone(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)
    try:
        fact = session.memory.save("preference", "synthetic secret-ish value")
        session.memory.delete(fact.fact_id)
        assert list(session.memory.tombstones()) == [fact.fact_id]
        rows = session.storage.query("SELECT * FROM memory_tombstones")
        assert all("synthetic" not in str(cell) for row in rows for cell in row)
    finally:
        session.close()


def test_save_and_correct_refused_after_concurrent_disable_commits(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(data_dir=tmp_path / "data", config_path=None)
    session_b = AwakeningSession(cfg, provider=FakeProvider())
    session_b.open()
    try:
        name_fact = session_b.memory.save("preferred_name", "Zorblax")

        # Hold an uncommitted BEGIN IMMEDIATE that disables memory in connection B
        # while a second connection (A, in its own thread) attempts save(); once B
        # commits, A must refuse.
        save_errors: list[BaseException] = []
        a_ready = threading.Event()
        go_save = threading.Event()
        a_outcome: list[bool] = []

        def _thread_a_save() -> None:
            session_a = AwakeningSession(cfg, provider=FakeProvider())
            session_a.open()
            try:
                a_ready.set()
                assert go_save.wait(timeout=5.0)
                try:
                    session_a.memory.save("preference", "blocked after disable")
                except BaseException as exc:
                    save_errors.append(exc)
                turn = session_a.handle_turn("What is my preferred name?")
                a_outcome.append(
                    turn.facts_used == ()
                    and "I don't have a confirmed preferred name" in turn.reply
                )
            finally:
                session_a.close()

        worker = threading.Thread(target=_thread_a_save)
        worker.start()
        assert a_ready.wait(timeout=5.0)

        with session_b.storage.transaction("disable in progress"):
            session_b.storage._require_conn().execute(
                "UPDATE memory_settings SET enabled = 0, context_floor = ?, updated_at = ?",
                ("2099-01-01T00:00:00.000000Z", "2099-01-01T00:00:00.000000Z"),
            )
            go_save.set()
            worker.join(timeout=0.2)
            assert worker.is_alive()

        worker.join(timeout=5.0)
        assert not worker.is_alive()
        assert len(save_errors) == 1 and isinstance(save_errors[0], MemoryDisabledError)
        assert a_outcome == [True]
        assert [f.value for f in session_b.memory.list_facts()] == ["Zorblax"]

        # Re-enable, then repeat the overlapping write-lock test for correct().
        session_b.memory.set_enabled(True)
        correct_errors: list[BaseException] = []
        a2_ready = threading.Event()
        go_correct = threading.Event()
        a2_outcome: list[bool] = []

        def _thread_a_correct() -> None:
            session_a = AwakeningSession(cfg, provider=FakeProvider())
            session_a.open()
            try:
                a2_ready.set()
                assert go_correct.wait(timeout=5.0)
                try:
                    session_a.memory.correct(name_fact.fact_id, "Quillon")
                except BaseException as exc:
                    correct_errors.append(exc)
                turn = session_a.handle_turn("What is my name?")
                a2_outcome.append(turn.facts_used == () and "Zorblax" not in turn.reply)
            finally:
                session_a.close()

        worker2 = threading.Thread(target=_thread_a_correct)
        worker2.start()
        assert a2_ready.wait(timeout=5.0)

        with session_b.storage.transaction("disable during correct"):
            session_b.storage._require_conn().execute(
                "UPDATE memory_settings SET enabled = 0, context_floor = ?, updated_at = ?",
                ("2099-01-02T00:00:00.000000Z", "2099-01-02T00:00:00.000000Z"),
            )
            go_correct.set()
            worker2.join(timeout=0.2)
            assert worker2.is_alive()

        worker2.join(timeout=5.0)
        assert not worker2.is_alive()
        assert len(correct_errors) == 1 and isinstance(
            correct_errors[0], MemoryDisabledError
        )
        assert a2_outcome == [True]
        assert [f.value for f in session_b.memory.list_facts()] == ["Zorblax"]
    finally:
        session_b.close()


def test_load_context_reads_one_consistent_snapshot_per_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = AppConfig(data_dir=tmp_path / "data", config_path=None)
    session_a = AwakeningSession(cfg, provider=FakeProvider())
    session_a.open()
    try:
        name_fact = session_a.memory.save("preferred_name", "Zorblax")
        session_a.handle_turn("warm up")

        b_ready = threading.Event()
        in_snapshot = threading.Event()
        b_attempting = threading.Event()
        disable_done = threading.Event()
        real_list = MemoryStore._list_facts

        def _paused_list(
            store: MemoryStore, conn: sqlite3.Connection
        ) -> tuple[MemoryFact, ...]:
            if store is session_a.memory:
                in_snapshot.set()
                assert b_attempting.wait(timeout=5.0)
                # Give connection B time to reach COMMIT; it cannot complete
                # while A holds its consistent SHARED read snapshot.
                time.sleep(0.15)
                assert not disable_done.is_set()
            return real_list(store, conn)

        monkeypatch.setattr(MemoryStore, "_list_facts", _paused_list)

        def _disable_from_b() -> None:
            session_b = AwakeningSession(cfg, provider=FakeProvider())
            session_b.open()
            try:
                b_ready.set()
                assert in_snapshot.wait(timeout=5.0)
                b_attempting.set()
                session_b.memory.set_enabled(False)
                disable_done.set()
            finally:
                session_b.close()

        disabler = threading.Thread(target=_disable_from_b)
        disabler.start()
        assert b_ready.wait(timeout=5.0)

        # Connection A reads one consistent pre-commit snapshot (enabled=True + Zorblax).
        settings, selection = session_a.memory.load_context()
        assert settings.enabled is True
        assert tuple(fact.value for fact in selection.facts) == ("Zorblax",)

        disabler.join(timeout=5.0)
        assert not disabler.is_alive()
        assert disable_done.is_set()

        # Once disabling committed, A's save/correct are refused and A's next turn
        # gets no facts and resets the open chat window.
        with pytest.raises(MemoryDisabledError):
            session_a.memory.save("preference", "refused")
        with pytest.raises(MemoryDisabledError):
            session_a.memory.correct(name_fact.fact_id, "Quillon")
        outcome = session_a.handle_turn("What is my preferred name?")
        assert outcome.facts_used == ()
        assert outcome.history_turns_used == 0
        assert "I don't have a confirmed preferred name" in outcome.reply
    finally:
        in_snapshot.set()
        b_attempting.set()
        session_a.close()


def test_fake_provider_preferred_name_recall_and_transcript_separation(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(data_dir=tmp_path / "data", config_path=None)
    cfg = AppConfig(
        data_dir=tmp_path / "data",
        config_path=None,
        history=cfg.history.__class__(enabled=True),
    )
    session = AwakeningSession(cfg, provider=FakeProvider())
    session.open()
    try:
        missing = session.handle_turn("What is my preferred name?")
        assert "I don't have a confirmed preferred name" in missing.reply

        fact = session.memory.save("preferred_name", "Zorblax")
        recalled = session.handle_turn("What is my preferred name?")
        assert "Your confirmed preferred name is Zorblax" in recalled.reply
        # The persisted assistant transcript must not store another copy of the name.
        assert "Zorblax" not in session._session_messages[-1].content

        session.memory.correct(fact.fact_id, "Quillon")
        edited = session.handle_turn("What should you call me?")
        assert "Your confirmed preferred name is Quillon" in edited.reply
        assert "Zorblax" not in edited.reply
        assert "Quillon" not in session._session_messages[-1].content

        session.memory.set_enabled(False)
        muted = session.handle_turn("Who am I?")
        assert "I don't have a confirmed preferred name" in muted.reply
        assert "Quillon" not in muted.reply

        session.memory.set_enabled(True)
        session.memory.delete(fact.fact_id)
        deleted = session.handle_turn("Do you remember my preferred name?")
        assert "I don't have a confirmed preferred name" in deleted.reply
        assert "Quillon" not in deleted.reply
    finally:
        session.close()

    conn = sqlite3.connect(str(cfg.data_dir / "bunny.sqlite3"))
    try:
        rows = conn.execute("SELECT assistant_text FROM turns").fetchall()
        assert len(rows) == 5
        assert all("Zorblax" not in text and "Quillon" not in text for (text,) in rows)
    finally:
        conn.close()
