"""In-process tests for bounded context and next-turn effect of memory changes."""

from __future__ import annotations

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
)
from bunny_ai.providers import ProviderRequest, ProviderResponse


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
