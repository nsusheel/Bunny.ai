"""TOML configuration loading, validation, and data-directory helpers."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bunny_ai.errors import ConfigError, StorageError

_ALLOWED_SECTIONS = frozenset({"storage", "history", "onboarding", "gateway", "budget"})
_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "storage": frozenset({"data_dir", "busy_timeout_ms"}),
    "history": frozenset({"enabled", "retention_days", "max_context_turns"}),
    "onboarding": frozenset({"ask_intro_question"}),
    "gateway": frozenset(
        {"profile", "output_limit", "request_timeout_seconds", "pricing_file"}
    ),
    "budget": frozenset({"per_request_micro_usd", "cumulative_micro_usd"}),
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
MIN_OUTPUT_LIMIT = 1
MAX_OUTPUT_LIMIT = 1_000_000
MIN_REQUEST_TIMEOUT_SECONDS = 1
MAX_REQUEST_TIMEOUT_SECONDS = 300


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
class GatewaySettings:
    """Validated settings under ``[gateway]``."""

    profile: str = "sim-default"
    output_limit: int = 1_024
    request_timeout_seconds: int = 30
    pricing_file: Path | None = None


@dataclass(frozen=True)
class BudgetSettings:
    """Validated settings under ``[budget]``."""

    per_request_micro_usd: int | None = None
    cumulative_micro_usd: int | None = None


@dataclass(frozen=True)
class AppConfig:
    """Resolved runtime configuration for Bunny.ai."""

    data_dir: Path
    config_path: Path | None
    storage: StorageSettings = field(default_factory=StorageSettings)
    history: HistorySettings = field(default_factory=HistorySettings)
    onboarding: OnboardingSettings = field(default_factory=OnboardingSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)


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


def _require_non_negative_int_or_none(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Setting '{field_name}' must be an integer.")
    if value < 0:
        raise ConfigError(f"Setting '{field_name}' must be non-negative.")
    return value


def _validate_table(parsed: dict[str, object], section: str) -> dict[str, object]:
    raw = parsed.get(section, {})
    if not isinstance(raw, dict):
        raise ConfigError(f"Section '[{section}]' must be a TOML table.")
    for key in raw:
        _check_not_secret_key(key)
        if key not in _ALLOWED_KEYS[section]:
            raise ConfigError(f"Unknown setting '{section}.{key}'.")
    return raw


def _validate_sections(
    parsed: dict[str, object], *, config_dir: Path
) -> tuple[
    StorageSettings,
    HistorySettings,
    OnboardingSettings,
    GatewaySettings,
    BudgetSettings,
]:
    for key in parsed:
        _check_not_secret_key(key)
        if key not in _ALLOWED_SECTIONS:
            raise ConfigError(f"Unknown configuration section '[{key}]'.")

    storage_section = _validate_table(parsed, "storage")
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

    history_section = _validate_table(parsed, "history")
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

    onboarding_section = _validate_table(parsed, "onboarding")
    onboarding_settings = OnboardingSettings(
        ask_intro_question=_require_bool(
            onboarding_section.get("ask_intro_question", True),
            field_name="onboarding.ask_intro_question",
        )
    )

    gateway_section = _validate_table(parsed, "gateway")
    raw_profile = gateway_section.get("profile", "sim-default")
    if not isinstance(raw_profile, str) or "\x00" in raw_profile:
        raise ConfigError("Setting 'gateway.profile' must be a valid string.")
    profile_name = raw_profile.strip()
    if not profile_name:
        raise ConfigError("Setting 'gateway.profile' must not be empty.")

    raw_pricing_file = gateway_section.get("pricing_file")
    parsed_pricing_file: Path | None = None
    if raw_pricing_file is not None:
        if not isinstance(raw_pricing_file, str):
            raise ConfigError("Setting 'gateway.pricing_file' must be a string.")
        pricing_candidate = _validate_path_string(
            raw_pricing_file, label="gateway.pricing_file"
        )
        parsed_pricing_file = (
            pricing_candidate
            if pricing_candidate.is_absolute()
            else (config_dir / pricing_candidate)
        ).resolve()

    gateway_settings = GatewaySettings(
        profile=profile_name,
        output_limit=_require_int_in_range(
            gateway_section.get("output_limit", 1_024),
            field_name="gateway.output_limit",
            minimum=MIN_OUTPUT_LIMIT,
            maximum=MAX_OUTPUT_LIMIT,
        ),
        request_timeout_seconds=_require_int_in_range(
            gateway_section.get("request_timeout_seconds", 30),
            field_name="gateway.request_timeout_seconds",
            minimum=MIN_REQUEST_TIMEOUT_SECONDS,
            maximum=MAX_REQUEST_TIMEOUT_SECONDS,
        ),
        pricing_file=parsed_pricing_file,
    )

    budget_section = _validate_table(parsed, "budget")
    budget_settings = BudgetSettings(
        per_request_micro_usd=_require_non_negative_int_or_none(
            budget_section.get("per_request_micro_usd"),
            field_name="budget.per_request_micro_usd",
        ),
        cumulative_micro_usd=_require_non_negative_int_or_none(
            budget_section.get("cumulative_micro_usd"),
            field_name="budget.cumulative_micro_usd",
        ),
    )

    return (
        storage_settings,
        history_settings,
        onboarding_settings,
        gateway_settings,
        budget_settings,
    )


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
        (
            storage_settings,
            history_settings,
            onboarding_settings,
            gateway_settings,
            budget_settings,
        ) = _validate_sections(parsed, config_dir=resolved_config_path.parent)
    else:
        storage_settings = StorageSettings()
        history_settings = HistorySettings()
        onboarding_settings = OnboardingSettings()
        gateway_settings = GatewaySettings()
        budget_settings = BudgetSettings()

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
        gateway=gateway_settings,
        budget=budget_settings,
    )
