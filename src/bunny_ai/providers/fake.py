"""Deterministic offline fake provider for the Awakening scaffold."""

from __future__ import annotations

import re

from bunny_ai.providers.types import ProviderRequest, ProviderResponse

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
