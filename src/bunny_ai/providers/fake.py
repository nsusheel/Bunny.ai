"""Deterministic offline fake provider for the Awakening scaffold."""

from __future__ import annotations

from bunny_ai.providers.types import ProviderRequest, ProviderResponse


class FakeProvider:
    """Credential-free, network-free simulated provider."""

    mode = "offline-simulated"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return an honest simulated reply reflecting active context bounds.

        The reply reports only counts. Fact values are never echoed into reply
        text, so saved transcripts cannot re-supply a fact the user later
        corrects or deletes.
        """
        history_turns = len(request.history) // 2
        fact_count = len(request.facts)
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
