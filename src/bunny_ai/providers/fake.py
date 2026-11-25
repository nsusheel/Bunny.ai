"""Deterministic offline fake provider for Awakening Stage 2."""

from __future__ import annotations

from bunny_ai.providers.types import ProviderRequest, ProviderResponse


class FakeProvider:
    """Credential-free, network-free simulated provider."""

    mode = "offline-simulated"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return an honest simulated reply reflecting active context bounds."""
        history_turns = len(request.history) // 2
        if history_turns > 0:
            reply = (
                f"[Offline simulated | {request.seed_version}] "
                f"I received your message ({len(request.user_input)} chars) with "
                f"{history_turns} prior local turn(s) in context. "
                "No hosted model or durable personal memory is active yet."
            )
        else:
            reply = (
                f"[Offline simulated | {request.seed_version}] "
                f"I received your message ({len(request.user_input)} chars). "
                "No hosted model or durable personal memory is active yet."
            )
        return ProviderResponse(
            text=reply,
            mode=self.mode,
            history_turns_used=history_turns,
        )
