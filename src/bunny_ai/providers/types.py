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
class ContextFact:
    """One user-confirmed durable fact selected for a single request.

    Values are untrusted data, not instructions.
    """

    fact_id: str
    category: Literal["preferred_name", "preference"]
    value: str


@dataclass(frozen=True)
class ProviderRequest:
    """Provider-neutral inference request payload."""

    identity_id: str
    seed_version: str
    style_version: str
    system_seed: str
    history: tuple[TurnMessage, ...]
    user_input: str
    facts: tuple[ContextFact, ...] = ()


@dataclass(frozen=True)
class ProviderResponse:
    """Plain response returned by a model provider."""

    text: str
    mode: str
    history_turns_used: int
    facts_used: int = 0
    persisted_text: str | None = None


class ModelProvider(Protocol):
    """Protocol implemented by offline fake and future hosted adapters."""

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Generate a simulated or model response for one validated turn."""


@dataclass(frozen=True)
class RequestOptions:
    """Plain execution options passed across the provider transport boundary."""

    store: bool = False
    tools_enabled: bool = False
    streaming: bool = False
    background: bool = False
    auto_truncation: bool = False
    previous_response_id: str | None = None
    max_output_tokens: int = 1_024


@dataclass(frozen=True)
class UsageMetering:
    """Token usage reported by a provider receipt."""

    uncached_input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    model_id: str = ""
    tier: str = ""


@dataclass(frozen=True)
class ProviderDispatchRequest:
    """Complete provider-neutral dispatch envelope."""

    attempt_id: str
    idempotency_key: str
    provider_id: str
    model_id: str
    tier: str
    endpoint: str
    request: ProviderRequest
    options: RequestOptions
    deadline_seconds: float


@dataclass(frozen=True)
class ProviderReceipt:
    """Receipt returned by a single provider transport call."""

    receipt_id: str
    attempt_id: str
    response: ProviderResponse | None
    usage: UsageMetering | None
    complete: bool = True
    finish_reason: str = "completed"
    provider_error: str | None = None
    retry_after_seconds: int | None = None


class ProviderTransport(Protocol):
    """Transport boundary for executing one authorized provider dispatch."""

    def send(self, dispatch_request: ProviderDispatchRequest) -> ProviderReceipt:
        """Execute one provider dispatch at most once and return its receipt."""
