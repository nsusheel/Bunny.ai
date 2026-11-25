"""Plain contracts for model providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class TurnMessage:
    """Single message entry passed to a model provider."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ProviderRequest:
    """Provider-neutral inference request payload."""

    identity_id: str
    seed_version: str
    style_version: str
    system_seed: str
    history: tuple[TurnMessage, ...]
    user_input: str


@dataclass(frozen=True)
class ProviderResponse:
    """Plain response returned by a model provider."""

    text: str
    mode: str
    history_turns_used: int


class ModelProvider(Protocol):
    """Protocol implemented by offline fake and future hosted adapters."""

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Generate a simulated or model response for one validated turn."""
