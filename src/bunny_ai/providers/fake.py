"""Deterministic offline fake provider for the Awakening scaffold."""

from __future__ import annotations

import re
import uuid

from bunny_ai.providers.types import (
    ModelProvider,
    ProviderDispatchRequest,
    ProviderReceipt,
    ProviderRequest,
    ProviderResponse,
    UsageMetering,
)

_NAME_QUESTION_RE = re.compile(
    r"\b(?:"
    r"what(?:'s|s|\s+is)\s+my\s+(?:preferred\s+)?name"
    r"|who\s+am\s+i"
    r"|what\s+(?:should|do)\s+you\s+call\s+me"
    r"|how\s+(?:should|do)\s+you\s+(?:call|address)\s+me"
    r"|do\s+you\s+(?:know|remember)\s+my\s+(?:preferred\s+)?name"
    r")\b",
    re.IGNORECASE,
)


def _is_preferred_name_question(text: str) -> bool:
    return _NAME_QUESTION_RE.search(text) is not None


class FakeProvider:
    """Credential-free, network-free simulated provider."""

    mode = "offline-simulated"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return an honest simulated reply reflecting active context bounds.

        When the user asks directly for their preferred name, the displayed
        reply uses the confirmed preferred name supplied in ``request.facts``.
        ``persisted_text`` omits the name value so saved transcripts cannot
        re-supply a fact the user later corrects or deletes.
        """
        history_turns = len(request.history) // 2
        fact_count = len(request.facts)
        if _is_preferred_name_question(request.user_input):
            summary_parts = []
            if history_turns > 0:
                summary_parts.append(f"{history_turns} prior local turn(s) in context")
            summary_parts.append(f"{fact_count} confirmed memory fact(s) supplied")
            summary = "; ".join(summary_parts)
            preferred_name = next(
                (f.value for f in request.facts if f.category == "preferred_name"),
                None,
            )
            if preferred_name is not None:
                displayed = (
                    f"[Offline simulated | {request.seed_version}] "
                    f"Your confirmed preferred name is {preferred_name} ({summary}). "
                    "No hosted model is active."
                )
                persisted: str | None = (
                    f"[Offline simulated | {request.seed_version}] "
                    "Answered a preferred-name question using the confirmed "
                    f"preferred name supplied for that turn ({summary}; value omitted "
                    "from saved transcript). No hosted model is active."
                )
            else:
                displayed = (
                    f"[Offline simulated | {request.seed_version}] "
                    "I don't have a confirmed preferred name for you right now "
                    f"({summary}). No hosted model is active."
                )
                persisted = None
            return ProviderResponse(
                text=displayed,
                mode=self.mode,
                history_turns_used=history_turns,
                facts_used=fact_count,
                persisted_text=persisted,
            )

        parts = [
            f"[Offline simulated | {request.seed_version}] "
            f"I received your message ({len(request.user_input)} chars)"
        ]
        if history_turns > 0:
            parts.append(f" with {history_turns} prior local turn(s) in context")
        parts.append(f"; {fact_count} confirmed memory fact(s) supplied. ")
        parts.append("No hosted model is active.")
        return ProviderResponse(
            text="".join(parts),
            mode=self.mode,
            history_turns_used=history_turns,
            facts_used=fact_count,
        )


class SimulatedTransport:
    """Offline simulated transport wrapping a local ``ModelProvider``."""

    def __init__(self, provider: ModelProvider | None = None) -> None:
        self.provider: ModelProvider = (
            provider if provider is not None else FakeProvider()
        )
        self.call_count: int = 0
        self.dispatched_requests: list[ProviderDispatchRequest] = []

    @property
    def requests(self) -> list[ProviderDispatchRequest]:
        """Alias for ``dispatched_requests``."""
        return self.dispatched_requests

    def send(self, dispatch_request: ProviderDispatchRequest) -> ProviderReceipt:
        """Execute one simulated dispatch and return a deterministic receipt."""
        self.call_count += 1
        self.dispatched_requests.append(dispatch_request)
        req = dispatch_request.request
        response = self.provider.complete(req)

        input_bytes = (
            len(req.system_seed.encode("utf-8"))
            + len(req.user_input.encode("utf-8"))
            + sum(
                len(msg.role.encode("utf-8")) + len(msg.content.encode("utf-8"))
                for msg in req.history
            )
            + sum(
                len(fact.category.encode("utf-8")) + len(fact.value.encode("utf-8"))
                for fact in req.facts
            )
        )
        uncached_input_tokens = ((input_bytes + 3) // 4) + 32
        output_bytes = len(response.text.encode("utf-8"))
        max_out = max(0, dispatch_request.options.max_output_tokens)
        raw_output_tokens = max(1, (output_bytes + 3) // 4)
        output_tokens = min(max_out, raw_output_tokens) if max_out > 0 else 0

        usage = UsageMetering(
            uncached_input_tokens=uncached_input_tokens,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=output_tokens,
            reasoning_tokens=0,
            model_id=dispatch_request.model_id,
            tier=dispatch_request.tier,
        )
        return ProviderReceipt(
            receipt_id=f"sim-rcpt-{uuid.uuid4().hex}",
            attempt_id=dispatch_request.attempt_id,
            response=response,
            usage=usage,
            complete=True,
            finish_reason="completed",
        )
