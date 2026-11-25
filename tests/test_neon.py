import pytest

from bunny_ai.neon import (
    MAX_INPUT_CHARS,
    SEED_VERSION,
    STYLE_VERSION,
    SeedPersonality,
    format_awakening_greeting,
    format_returning_greeting,
    sanitize_terminal_text,
    validate_user_input,
)


def test_seed_personality_version_and_rendering() -> None:
    seed = SeedPersonality()
    assert seed.seed_version == SEED_VERSION
    assert seed.style_version == STYLE_VERSION
    rendered = seed.render_system_seed()
    assert SEED_VERSION in rendered
    assert "honesty" in rendered
    assert "privacy" in rendered


def test_greetings_distinguish_first_launch_and_restart() -> None:
    first_with_q = format_awakening_greeting(ask_intro_question=True)
    assert "offline simulated mode" in first_with_q
    assert "/skip" in first_with_q

    first_without_q = format_awakening_greeting(ask_intro_question=False)
    assert "offline simulated mode" in first_without_q
    assert "/skip" not in first_without_q

    returning = format_returning_greeting(history_enabled=True, restored_turns=3)
    assert "Welcome back!" in returning
    assert "3 prior turn(s)" in returning


def test_sanitize_terminal_text_strips_ansi_and_control_chars() -> None:
    raw = "Hello \x1b[31mred\x1b[0m\x1b]0;pwn\x07 world\x08!\r\n\tNext\x9b2J"
    cleaned = sanitize_terminal_text(raw)
    assert "\x1b" not in cleaned
    assert "\x07" not in cleaned
    assert "\x08" not in cleaned
    assert "\x9b" not in cleaned
    assert "Hello red world" in cleaned
    assert "\n\tNext" in cleaned


def test_validate_user_input_bounds() -> None:
    assert validate_user_input("  hi Bunny  ") == "hi Bunny"
    with pytest.raises(ValueError, match="NUL"):
        validate_user_input("hi\x00there")
    with pytest.raises(ValueError, match="non-whitespace"):
        validate_user_input("   \n\t ")
    with pytest.raises(ValueError, match="maximum length"):
        validate_user_input("a" * (MAX_INPUT_CHARS + 1))
