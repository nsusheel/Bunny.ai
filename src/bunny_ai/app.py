"""Application orchestration for Awakening Stage 3."""

from __future__ import annotations

from dataclasses import dataclass

from bunny_ai.config import AppConfig
from bunny_ai.memory import MemoryStore
from bunny_ai.neon import (
    SeedPersonality,
    format_awakening_greeting,
    format_returning_greeting,
    sanitize_terminal_text,
    validate_user_input,
)
from bunny_ai.providers import (
    ContextFact,
    FakeProvider,
    ModelProvider,
    ProviderRequest,
    TurnMessage,
)
from bunny_ai.storage import (
    IdentityRecord,
    InitResult,
    LocalStorage,
    StorageSnapshot,
)


@dataclass(frozen=True)
class TurnOutcome:
    """Result of one simulated offline conversation turn."""

    reply: str
    mode: str
    history_turns_used: int
    persisted: bool
    facts_used: tuple[ContextFact, ...] = ()


def run_demo(text: str) -> str:
    """Run the stateless Stage 1 simulated demo without writing any files."""
    return f"[Offline demo, simulated] Received {len(text)} characters."


def get_status(config: AppConfig) -> StorageSnapshot:
    """Inspect local state read-only without creating files or directories."""
    storage = LocalStorage(
        config.data_dir,
        busy_timeout_ms=config.storage.busy_timeout_ms,
    )
    return storage.inspect()


class AwakeningSession:
    """Manages one offline Bunny.ai session lifecycle."""

    def __init__(
        self,
        config: AppConfig,
        *,
        history_override: bool | None = None,
        provider: ModelProvider | None = None,
    ) -> None:
        self.config = config
        self.history_enabled = (
            config.history.enabled if history_override is None else history_override
        )
        self.history_overridden = history_override is not None
        self.personality = SeedPersonality()
        self.provider: ModelProvider = (
            provider if provider is not None else FakeProvider()
        )
        self.storage = LocalStorage(
            config.data_dir,
            busy_timeout_ms=config.storage.busy_timeout_ms,
        )
        self._init_result: InitResult | None = None
        self._identity: IdentityRecord | None = None
        self._memory: MemoryStore | None = None
        self._seen_floor: str | None = None
        self._history_session_id: str | None = None
        self._session_messages: list[TurnMessage] = []
        self._persisted_turn_index = 0
        self._restored_turn_count = 0
        self._pruned_turn_count = 0
        self._closed = False

    def __enter__(self) -> AwakeningSession:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def init_result(self) -> InitResult:
        if self._init_result is None:
            raise RuntimeError("AwakeningSession has not been opened.")
        return self._init_result

    @property
    def identity(self) -> IdentityRecord:
        if self._identity is None:
            raise RuntimeError("AwakeningSession has not been opened.")
        return self._identity

    @property
    def restored_turn_count(self) -> int:
        return self._restored_turn_count

    @property
    def pruned_turn_count(self) -> int:
        return self._pruned_turn_count

    def open(self) -> InitResult:
        """Initialize or load the local identity and optional prior turns."""
        result = self.storage.initialize_or_load(
            seed_version=self.personality.seed_version,
            style_version=self.personality.style_version,
        )
        self._init_result = result
        self._identity = result.identity
        self._memory = MemoryStore(self.storage, result.identity.identity_id)
        self._seen_floor = self._memory.settings().context_floor
        if not result.is_first_launch:
            self._pruned_turn_count = self.storage.prune_expired_history(
                result.identity.identity_id,
                retention_days=self.config.history.retention_days,
            )
        if self.history_enabled and not result.is_first_launch:
            recent = self.storage.load_recent_turns(
                result.identity.identity_id,
                limit=self.config.history.max_context_turns,
                retention_days=self.config.history.retention_days,
                not_before=self._seen_floor,
            )
            self._restored_turn_count = len(recent)
            for turn in recent:
                self._session_messages.append(
                    TurnMessage(role="user", content=turn.user_text)
                )
                self._session_messages.append(
                    TurnMessage(role="assistant", content=turn.assistant_text)
                )
        return result

    def greeting_text(self, *, skip_intro: bool = False) -> str:
        """Return the appropriate first-launch or returning greeting."""
        if self.init_result.is_first_launch:
            ask_intro = self.config.onboarding.ask_intro_question and not skip_intro
            return format_awakening_greeting(ask_intro_question=ask_intro)
        return format_returning_greeting(
            history_enabled=self.history_enabled,
            restored_turns=self._restored_turn_count,
        )

    def skip_onboarding(self) -> None:
        """Mark the optional Awakening intro question as skipped."""
        if (
            self.init_result.is_first_launch
            and self.identity.onboarding_state == "greeted"
        ):
            self._identity = self.storage.update_onboarding_state(
                self.identity.identity_id, "skipped"
            )

    def handle_turn(self, raw_text: str, *, from_intro: bool = False) -> TurnOutcome:
        """Process one simulated turn and persist only if opt-in history is enabled."""
        clean_input = validate_user_input(raw_text)
        if (
            from_intro
            and self.init_result.is_first_launch
            and self.identity.onboarding_state == "greeted"
        ):
            self._identity = self.storage.update_onboarding_state(
                self.identity.identity_id, "answered"
            )

        max_messages = self.config.history.max_context_turns * 2
        # Facts and the memory switch are re-read every turn, so a correction,
        # deletion or revocation (here or in another process) applies at once.
        settings, selection = self.memory.load_context()
        if settings.context_floor != self._seen_floor:
            self._seen_floor = settings.context_floor
            self._session_messages.clear()
        context_history = tuple(self._session_messages[-max_messages:])

        request = ProviderRequest(
            identity_id=self.identity.identity_id,
            seed_version=self.personality.seed_version,
            style_version=self.personality.style_version,
            system_seed=self.personality.render_system_seed(),
            history=context_history,
            user_input=clean_input,
            facts=selection.facts,
        )
        response = self.provider.complete(request)
        safe_reply = sanitize_terminal_text(response.text)
        safe_persisted_reply = (
            sanitize_terminal_text(response.persisted_text)
            if response.persisted_text is not None
            else safe_reply
        )

        self._session_messages.append(TurnMessage(role="user", content=clean_input))
        self._session_messages.append(
            TurnMessage(role="assistant", content=safe_persisted_reply)
        )

        persisted = False
        if self.history_enabled:
            if self._history_session_id is None:
                self._history_session_id = self.storage.start_history_session(
                    self.identity.identity_id
                )
            self._persisted_turn_index += 1
            self.storage.record_turn(
                session_id=self._history_session_id,
                turn_index=self._persisted_turn_index,
                user_text=clean_input,
                assistant_text=safe_persisted_reply,
                provider_mode=response.mode,
            )
            persisted = True

        return TurnOutcome(
            reply=safe_reply,
            mode=response.mode,
            history_turns_used=response.history_turns_used,
            persisted=persisted,
            facts_used=selection.facts,
        )

    @property
    def memory(self) -> MemoryStore:
        if self._memory is None:
            raise RuntimeError("AwakeningSession has not been opened.")
        return self._memory

    def clear_history(self) -> tuple[int, int]:
        """Delete stored transcripts and restart this chat's window.

        Durable memory facts and the memory switch are not touched.
        """
        turns, sessions = self.storage.clear_history(self.identity.identity_id)
        self._session_messages.clear()
        self._history_session_id = None
        self._persisted_turn_index = 0
        return turns, sessions

    def history_counts(self) -> tuple[int, int]:
        """Return (stored turns, turns eligible for automatic context reuse)."""
        identity_id = self.identity.identity_id
        floor = self.memory.settings().context_floor
        return (
            self.storage.count_turns(identity_id),
            self.storage.count_turns(identity_id, not_before=floor),
        )

    def close(self) -> None:
        """Finish any open history session and close local storage cleanly."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._history_session_id is not None:
                self.storage.end_history_session(self._history_session_id)
        finally:
            self.storage.close()
