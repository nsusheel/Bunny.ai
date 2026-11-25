"""TOML configuration loading, validation, and data-directory helpers."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bunny_ai.errors import ConfigError, StorageError

_ALLOWED_SECTIONS = frozenset({"storage", "history", "onboarding"})
_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "storage": frozenset({"data_dir", "busy_timeout_ms"}),
    "history": frozenset({"enabled", "retention_days", "max_context_turns"}),
    "onboarding": frozenset({"ask_intro_question"}),
}
_SECRET_TOKENS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "private_key",
    "credential",
)

MIN_BUSY_TIMEOUT_MS = 100
MAX_BUSY_TIMEOUT_MS = 30_000
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3_650
MIN_CONTEXT_TURNS = 1
MAX_CONTEXT_TURNS = 100


@dataclass(frozen=True)
class StorageSettings:
    """Validated settings under ``[storage]``."""

    data_dir: Path | None = None
    busy_timeout_ms: int = 5_000


@dataclass(frozen=True)
class HistorySettings:
    """Validated settings under ``[history]``."""

    enabled: bool = False
    retention_days: int = 30
    max_context_turns: int = 10


@dataclass(frozen=True)
class OnboardingSettings:
    """Validated settings under ``[onboarding]``."""

    ask_intro_question: bool = True


@dataclass(frozen=True)
class AppConfig:
    """Resolved runtime configuration for Bunny.ai."""

    data_dir: Path
    config_path: Path | None
    storage: StorageSettings = field(default_factory=StorageSettings)
    history: HistorySettings = field(default_factory=HistorySettings)
    onboarding: OnboardingSettings = field(default_factory=OnboardingSettings)


def default_config_dir() -> Path:
    """Return the platform-standard directory for Bunny.ai configuration."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "bunny-ai"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "bunny-ai"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "bunny-ai"


def default_data_dir() -> Path:
    """Return the platform-standard directory for Bunny.ai local state."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = (
            Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        )
        return base / "bunny-ai"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "bunny-ai"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "bunny-ai"


def restrict_path_permissions(path: Path, *, is_dir: bool) -> None:
    """Apply owner-only permissions on POSIX platforms."""
    if os.name == "posix":
        os.chmod(path, 0o700 if is_dir else 0o600)


def ensure_data_dir(path: Path) -> Path:
    """Create missing directories privately on POSIX while preserving existing modes."""
    if path.exists():
        if not path.is_dir():
            raise StorageError(f"Data path exists and is not a directory: {path}")
        return path

    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    if current.exists() and not current.is_dir():
        raise StorageError(f"Data path ancestor is not a directory: {current}")

    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        except FileExistsError:
            if not directory.is_dir():
                raise StorageError(
                    f"Data path exists and is not a directory: {directory}"
                ) from None
            continue
        except OSError as exc:
            raise StorageError(
                f"Cannot prepare data directory '{path}': {exc}"
            ) from exc
        try:
            restrict_path_permissions(directory, is_dir=True)
        except OSError as exc:
            raise StorageError(
                f"Cannot set permissions on data directory '{directory}': {exc}"
            ) from exc
    return path


def _validate_path_string(raw: str, *, label: str) -> Path:
    if "\x00" in raw:
        raise ConfigError(f"{label} must not contain NUL bytes.")
    stripped = raw.strip()
    if not stripped:
        raise ConfigError(f"{label} must not be empty.")
    return Path(stripped).expanduser()


def _check_not_secret_key(key: str) -> None:
    lowered = key.lower().replace("-", "_")
    if any(token in lowered for token in _SECRET_TOKENS):
        raise ConfigError(
            f"Secret-like key '{key}' is not permitted in TOML configuration."
        )


def _require_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"Setting '{field_name}' must be a boolean.")
    return value


def _require_int_in_range(
    value: object, *, field_name: str, minimum: int, maximum: int
) -> int:
    # Check bool before int because bool is a subclass of int in Python.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Setting '{field_name}' must be an integer.")
    if value < minimum or value > maximum:
        raise ConfigError(
            f"Setting '{field_name}' must be between {minimum} and {maximum}."
        )
    return value


def _parse_toml_file(config_path: Path) -> dict[str, object]:
    try:
        raw_bytes = config_path.read_bytes()
        text = raw_bytes.decode("utf-8")
        parsed = tomllib.loads(text)
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"Configuration file '{config_path}' is not valid UTF-8: {exc}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Configuration file '{config_path}' contains invalid TOML: {exc}"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Cannot read configuration file '{config_path}': {exc}"
        ) from exc
    return parsed


def _validate_sections(
    parsed: dict[str, object], *, config_dir: Path
) -> tuple[StorageSettings, HistorySettings, OnboardingSettings]:
    for key in parsed:
        _check_not_secret_key(key)
        if key not in _ALLOWED_SECTIONS:
            raise ConfigError(f"Unknown configuration section '[{key}]'.")

    storage_section = parsed.get("storage", {})
    if not isinstance(storage_section, dict):
        raise ConfigError("Section '[storage]' must be a TOML table.")
    for key in storage_section:
        _check_not_secret_key(key)
        if key not in _ALLOWED_KEYS["storage"]:
            raise ConfigError(f"Unknown setting 'storage.{key}'.")

    raw_data_dir = storage_section.get("data_dir")
    parsed_data_dir: Path | None = None
    if raw_data_dir is not None:
        if not isinstance(raw_data_dir, str):
            raise ConfigError("Setting 'storage.data_dir' must be a string.")
        candidate = _validate_path_string(raw_data_dir, label="storage.data_dir")
        parsed_data_dir = (
            candidate if candidate.is_absolute() else (config_dir / candidate)
        ).resolve()

    busy_timeout_ms = _require_int_in_range(
        storage_section.get("busy_timeout_ms", 5_000),
        field_name="storage.busy_timeout_ms",
        minimum=MIN_BUSY_TIMEOUT_MS,
        maximum=MAX_BUSY_TIMEOUT_MS,
    )
    storage_settings = StorageSettings(
        data_dir=parsed_data_dir,
        busy_timeout_ms=busy_timeout_ms,
    )

    history_section = parsed.get("history", {})
    if not isinstance(history_section, dict):
        raise ConfigError("Section '[history]' must be a TOML table.")
    for key in history_section:
        _check_not_secret_key(key)
        if key not in _ALLOWED_KEYS["history"]:
            raise ConfigError(f"Unknown setting 'history.{key}'.")

    history_settings = HistorySettings(
        enabled=_require_bool(
            history_section.get("enabled", False), field_name="history.enabled"
        ),
        retention_days=_require_int_in_range(
            history_section.get("retention_days", 30),
            field_name="history.retention_days",
            minimum=MIN_RETENTION_DAYS,
            maximum=MAX_RETENTION_DAYS,
        ),
        max_context_turns=_require_int_in_range(
            history_section.get("max_context_turns", 10),
            field_name="history.max_context_turns",
            minimum=MIN_CONTEXT_TURNS,
            maximum=MAX_CONTEXT_TURNS,
        ),
    )

    onboarding_section = parsed.get("onboarding", {})
    if not isinstance(onboarding_section, dict):
        raise ConfigError("Section '[onboarding]' must be a TOML table.")
    for key in onboarding_section:
        _check_not_secret_key(key)
        if key not in _ALLOWED_KEYS["onboarding"]:
            raise ConfigError(f"Unknown setting 'onboarding.{key}'.")

    onboarding_settings = OnboardingSettings(
        ask_intro_question=_require_bool(
            onboarding_section.get("ask_intro_question", True),
            field_name="onboarding.ask_intro_question",
        )
    )

    return storage_settings, history_settings, onboarding_settings


def load_config(
    *,
    cli_config_path: str | Path | None = None,
    cli_data_dir: str | Path | None = None,
) -> AppConfig:
    """Resolve and validate Bunny.ai settings from CLI flags, env, and TOML."""
    explicit_data_dir: Path | None = None
    if cli_data_dir is not None:
        explicit_data_dir = _validate_path_string(
            str(cli_data_dir), label="--data-dir"
        ).resolve()
    elif "BUNNY_DATA_DIR" in os.environ:
        explicit_data_dir = _validate_path_string(
            os.environ["BUNNY_DATA_DIR"], label="BUNNY_DATA_DIR"
        ).resolve()

    explicit_config: Path | None = None
    if cli_config_path is not None:
        explicit_config = _validate_path_string(
            str(cli_config_path), label="--config"
        ).resolve()
    elif "BUNNY_CONFIG" in os.environ:
        explicit_config = _validate_path_string(
            os.environ["BUNNY_CONFIG"], label="BUNNY_CONFIG"
        ).resolve()

    resolved_config_path: Path | None = None
    if explicit_config is not None:
        if not explicit_config.exists() or not explicit_config.is_file():
            raise ConfigError(
                f"Specified configuration file does not exist: {explicit_config}"
            )
        resolved_config_path = explicit_config
    else:
        candidate = (
            explicit_data_dir / "config.toml"
            if explicit_data_dir is not None
            else default_config_dir() / "config.toml"
        )
        if candidate.exists():
            if not candidate.is_file():
                raise ConfigError(
                    f"Configuration path is not a regular file: {candidate}"
                )
            resolved_config_path = candidate

    if resolved_config_path is not None:
        parsed = _parse_toml_file(resolved_config_path)
        storage_settings, history_settings, onboarding_settings = _validate_sections(
            parsed, config_dir=resolved_config_path.parent
        )
    else:
        storage_settings = StorageSettings()
        history_settings = HistorySettings()
        onboarding_settings = OnboardingSettings()

    if explicit_data_dir is not None:
        final_data_dir = explicit_data_dir
    elif storage_settings.data_dir is not None:
        final_data_dir = storage_settings.data_dir
    else:
        final_data_dir = default_data_dir().resolve()

    return AppConfig(
        data_dir=final_data_dir,
        config_path=resolved_config_path,
        storage=storage_settings,
        history=history_settings,
        onboarding=onboarding_settings,
    )
