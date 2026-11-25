"""Neon conversation role: versioned seed personality and text safety."""

from __future__ import annotations

import re
from dataclasses import dataclass

SEED_VERSION = "seed-1"
STYLE_VERSION = "style-1"
MAX_INPUT_CHARS = 16_384

# Match CSI, OSC, and simple 2-byte ESC sequences before filtering raw control chars.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)


@dataclass(frozen=True)
class SeedPersonality:
    """Layer 1 core values and Layer 2 initial style for Awakening."""

    seed_version: str = SEED_VERSION
    style_version: str = STYLE_VERSION
    core_values: tuple[str, ...] = (
        "honesty",
        "curiosity",
        "privacy",
        "autonomy",
        "least-privilege",
    )
    style_traits: tuple[str, ...] = (
        "warm",
        "concise",
        "observant",
        "non-intrusive",
    )

    def render_system_seed(self) -> str:
        """Render the immutable seed context sent to a provider."""
        values = ", ".join(self.core_values)
        traits = ", ".join(self.style_traits)
        return (
            f"Neon seed {self.seed_version} ({self.style_version}): "
            f"values=[{values}]; style=[{traits}]."
        )


def format_awakening_greeting(*, ask_intro_question: bool) -> str:
    """Build the first-launch greeting with an optional skippable question."""
    greeting = (
        "Hey! I'm Bunny, running in offline simulated mode. "
        "I don't know anything about you yet, and nothing personal is saved automatically."
    )
    if ask_intro_question:
        greeting += (
            "\nAnything you'd like to explore or start with today? "
            "(Optional -- press Enter or type /skip to skip, or /help for commands.)"
        )
    return greeting


def format_returning_greeting(*, history_enabled: bool, restored_turns: int) -> str:
    """Build the restart greeting without replaying first-run onboarding."""
    if history_enabled:
        return (
            "Welcome back! Restored your local Bunny identity "
            f"(offline simulated mode, {restored_turns} prior turn(s) loaded)."
        )
    return (
        "Welcome back! Restored your local Bunny identity "
        "(offline simulated mode, local history off)."
    )


def sanitize_terminal_text(text: str) -> str:
    """Strip terminal escape sequences and unsafe control characters."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    without_ansi = _ANSI_ESCAPE_RE.sub("", normalized)
    cleaned: list[str] = []
    for char in without_ansi:
        code = ord(char)
        if char in ("\n", "\t") or (
            code >= 0x20 and code != 0x7F and not 0x80 <= code <= 0x9F
        ):
            cleaned.append(char)
        else:
            cleaned.append("?")
    return "".join(cleaned)


def validate_user_input(text: str) -> str:
    """Validate interactive or single-turn user text input."""
    if "\x00" in text:
        raise ValueError("Input must not contain NUL bytes.")
    stripped = text.strip()
    if not stripped:
        raise ValueError("Input must contain at least one non-whitespace character.")
    if len(stripped) > MAX_INPUT_CHARS:
        raise ValueError(
            f"Input exceeds maximum length of {MAX_INPUT_CHARS} characters."
        )
    return stripped
