"""Integer micro-USD pricing, conservative reservation, and settlement math.

All currency calculations in Bunny.ai use exact integer micro-USD
(1,000,000 micro-USD == $1.00) with explicit ceiling division and zero floats.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from bunny_ai.errors import PricingError
from bunny_ai.providers.types import ProviderRequest, UsageMetering

MICRO_USD_PER_USD = 1_000_000
TOKENS_PER_MILLION = 1_000_000
ENVELOPE_OVERHEAD_TOKENS = 256
PER_ITEM_OVERHEAD_TOKENS = 16
DEFAULT_PER_REQUEST_MICRO_USD = 50_000
DEFAULT_CUMULATIVE_MICRO_USD = 500_000
DEFAULT_PROFILE_ID = "sim-default"

InputBoundMode = Literal["envelope_bytes", "context_ceiling"]


def _require_non_bool_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PricingError(f"{label} must be an integer.")
    return value


def ceil_div(numerator: int, denominator: int) -> int:
    """Return ceiling(numerator / denominator) using exact integer arithmetic."""
    num = _require_non_bool_int(numerator, label="numerator")
    den = _require_non_bool_int(denominator, label="denominator")
    if num < 0:
        raise ValueError("numerator must be non-negative.")
    if den <= 0:
        raise ValueError("denominator must be positive.")
    return (num + den - 1) // den


def format_micro_usd(amount: int) -> str:
    """Format integer micro-USD without floats (e.g. ``$0.000563``)."""
    val = _require_non_bool_int(amount, label="amount")
    sign = "-" if val < 0 else ""
    abs_val = -val if val < 0 else val
    dollars, remainder = divmod(abs_val, MICRO_USD_PER_USD)
    return f"{sign}${dollars}.{remainder:06d}"


@dataclass(frozen=True)
class PriceProfile:
    """Versioned synthetic pricing profile used for reservation and settlement."""

    profile_id: str
    provider_id: str
    model_id: str
    tier: str
    endpoint: str
    price_version: str
    valid_from_utc: str
    valid_until_utc: str
    input_micro_usd_per_1m: int
    cache_read_micro_usd_per_1m: int
    cache_write_micro_usd_per_1m: int
    output_micro_usd_per_1m: int
    max_input_tokens: int
    max_output_tokens: int
    max_input_chars: int
    text_only: bool = True
    input_bound_mode: InputBoundMode = "envelope_bytes"
    long_context_threshold_tokens: int | None = None
    long_context_input_micro_usd_per_1m: int | None = None
    long_context_output_micro_usd_per_1m: int | None = None


DEFAULT_PRICE_PROFILES: dict[str, PriceProfile] = {
    DEFAULT_PROFILE_ID: PriceProfile(
        profile_id=DEFAULT_PROFILE_ID,
        provider_id="simulated",
        model_id="sim-neon-1",
        tier="standard",
        endpoint="simulated://local/v1/responses",
        price_version="sim-2026-11-v1",
        valid_from_utc="2026-01-01T00:00:00Z",
        valid_until_utc="2027-12-31T23:59:59Z",
        input_micro_usd_per_1m=100_000,
        cache_read_micro_usd_per_1m=10_000,
        cache_write_micro_usd_per_1m=125_000,
        output_micro_usd_per_1m=500_000,
        max_input_tokens=4_096,
        max_output_tokens=1_024,
        max_input_chars=16_384,
    )
}


@dataclass(frozen=True)
class SettlementCalc:
    """Detailed integer micro-USD settlement calculation from a usage receipt."""

    uncached_input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_input_tokens: int
    long_context_applied: bool
    input_rate_micro_usd_per_1m: int
    cache_read_rate_micro_usd_per_1m: int
    cache_write_rate_micro_usd_per_1m: int
    output_rate_micro_usd_per_1m: int
    numerator_micro_usd_tokens: int
    settled_micro_usd: int

    @property
    def micro_usd(self) -> int:
        """Alias for ``settled_micro_usd``."""
        return self.settled_micro_usd


_REQUIRED_PRICE_STRINGS = (
    "profile_id",
    "provider_id",
    "model_id",
    "tier",
    "endpoint",
    "price_version",
    "valid_from_utc",
    "valid_until_utc",
)

_REQUIRED_PRICE_INTS = (
    "input_micro_usd_per_1m",
    "cache_read_micro_usd_per_1m",
    "cache_write_micro_usd_per_1m",
    "output_micro_usd_per_1m",
    "max_input_tokens",
    "max_output_tokens",
    "max_input_chars",
)


def _reject_json_constant(name: str) -> object:
    raise ValueError(f"Invalid JSON constant {name!r}")


def _parse_utc(ts: str) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def price_profile_to_json(profile: PriceProfile) -> str:
    """Serialize a ``PriceProfile`` deterministically to JSON."""
    return json.dumps(asdict(profile), sort_keys=True, ensure_ascii=False)


def price_profile_from_json(raw: str | bytes | Mapping[str, object]) -> PriceProfile:
    """Deserialize and type-validate a ``PriceProfile`` from JSON or mapping."""
    data: object
    if isinstance(raw, (str, bytes)):
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            data = json.loads(text, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError) as exc:
            raise PricingError(f"Invalid pricing JSON: {exc}") from exc
    else:
        data = raw

    if not isinstance(data, Mapping):
        raise PricingError("Price profile must be a JSON object.")

    fields: dict[str, Any] = {}
    for key in _REQUIRED_PRICE_STRINGS:
        val = data.get(key)
        if not isinstance(val, str) or not val.strip():
            raise PricingError(
                f"Price profile field '{key}' must be a non-empty string."
            )
        fields[key] = val.strip()

    for key in _REQUIRED_PRICE_INTS:
        if key not in data:
            raise PricingError(f"Price profile missing required field '{key}'.")
        fields[key] = _require_non_bool_int(data[key], label=key)

    text_only = data.get("text_only", True)
    if not isinstance(text_only, bool):
        raise PricingError("Price profile field 'text_only' must be a boolean.")
    fields["text_only"] = text_only

    mode = data.get("input_bound_mode", "envelope_bytes")
    if mode not in ("envelope_bytes", "context_ceiling"):
        raise PricingError("Price profile field 'input_bound_mode' is invalid.")
    fields["input_bound_mode"] = cast(InputBoundMode, mode)

    for opt_key in (
        "long_context_threshold_tokens",
        "long_context_input_micro_usd_per_1m",
        "long_context_output_micro_usd_per_1m",
    ):
        opt_val = data.get(opt_key)
        if opt_val is not None:
            fields[opt_key] = _require_non_bool_int(opt_val, label=opt_key)
        else:
            fields[opt_key] = None

    return PriceProfile(**fields)


def load_pricing_file(path: Path) -> dict[str, PriceProfile | None]:
    """Load a pricing JSON file, keeping malformed entries as ``None`` to fail closed."""
    try:
        raw_text = path.read_text(encoding="utf-8")
        parsed = json.loads(raw_text, parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PricingError(f"Cannot load pricing file '{path}': {exc}") from exc

    if not isinstance(parsed, dict):
        raise PricingError(f"Pricing file '{path}' must contain a JSON object.")

    if "profiles" in parsed:
        raw_profiles = parsed["profiles"]
        if not isinstance(raw_profiles, dict):
            raise PricingError(f"Pricing file '{path}' 'profiles' must be an object.")
    else:
        raw_profiles = parsed

    result: dict[str, PriceProfile | None] = {}
    for profile_id, entry in raw_profiles.items():
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise PricingError(f"Pricing file '{path}' contains an empty profile key.")
        if not isinstance(entry, Mapping):
            result[profile_id] = None
            continue
        merged = dict(entry)
        merged.setdefault("profile_id", profile_id)
        try:
            profile = price_profile_from_json(merged)
        except PricingError:
            result[profile_id] = None
            continue
        if profile.profile_id != profile_id:
            result[profile_id] = None
            continue
        result[profile_id] = profile
    return result


def validate_price_profile(
    profile: PriceProfile | None, now_utc: datetime
) -> tuple[str | None, str | None]:
    """Validate pricing completeness and UTC validity window; fail closed."""
    if profile is None:
        return ("pricing_missing", "No pricing profile is configured for dispatch.")

    for key in _REQUIRED_PRICE_STRINGS:
        val = getattr(profile, key)
        if not isinstance(val, str) or not val.strip():
            return ("pricing_incomplete", f"Pricing field '{key}' is missing or empty.")

    rates = (
        profile.input_micro_usd_per_1m,
        profile.cache_read_micro_usd_per_1m,
        profile.cache_write_micro_usd_per_1m,
        profile.output_micro_usd_per_1m,
    )
    if any(isinstance(r, bool) or not isinstance(r, int) or r < 0 for r in rates):
        return ("pricing_incomplete", "Pricing rates must be non-negative integers.")
    if sum(rates) <= 0:
        return (
            "pricing_incomplete",
            "Pricing profile has zero rates across all tiers.",
        )

    limits = (
        profile.max_input_tokens,
        profile.max_output_tokens,
        profile.max_input_chars,
    )
    if any(
        isinstance(lim, bool) or not isinstance(lim, int) or lim <= 0 for lim in limits
    ):
        return (
            "pricing_incomplete",
            "Pricing token/char limits must be positive integers.",
        )

    if profile.input_bound_mode not in ("envelope_bytes", "context_ceiling"):
        return ("pricing_incomplete", "Pricing input_bound_mode is invalid.")

    lc_fields = (
        profile.long_context_threshold_tokens,
        profile.long_context_input_micro_usd_per_1m,
        profile.long_context_output_micro_usd_per_1m,
    )
    if any(v is not None for v in lc_fields):
        if any(
            v is None or isinstance(v, bool) or not isinstance(v, int)
            for v in lc_fields
        ):
            return ("pricing_incomplete", "Long-context pricing tier is incomplete.")
        assert profile.long_context_threshold_tokens is not None
        assert profile.long_context_input_micro_usd_per_1m is not None
        assert profile.long_context_output_micro_usd_per_1m is not None
        if (
            profile.long_context_threshold_tokens <= 0
            or profile.long_context_input_micro_usd_per_1m < 0
            or profile.long_context_output_micro_usd_per_1m < 0
        ):
            return ("pricing_incomplete", "Long-context pricing values are invalid.")

    valid_from = _parse_utc(profile.valid_from_utc)
    valid_until = _parse_utc(profile.valid_until_utc)
    if valid_from is None or valid_until is None or valid_from >= valid_until:
        return ("pricing_incomplete", "Pricing validity timestamps are invalid.")

    now = (
        now_utc.replace(tzinfo=UTC)
        if now_utc.tzinfo is None
        else now_utc.astimezone(UTC)
    )
    if now < valid_from:
        return (
            "pricing_expired",
            f"Pricing profile '{profile.profile_id}' is not valid until {profile.valid_from_utc}.",
        )
    if now >= valid_until:
        return (
            "pricing_expired",
            f"Pricing profile '{profile.profile_id}' expired at {profile.valid_until_utc}.",
        )
    return (None, None)


def estimate_conservative_input_tokens(
    request: ProviderRequest, profile: PriceProfile
) -> int:
    """Compute a deterministic upper-bound input token count without floats."""
    if profile.input_bound_mode == "context_ceiling":
        return profile.max_input_tokens
    if profile.input_bound_mode != "envelope_bytes":
        raise PricingError(
            f"Unsupported input_bound_mode: {profile.input_bound_mode!r}"
        )

    total_tokens = ENVELOPE_OVERHEAD_TOKENS
    total_tokens += ceil_div(len(request.system_seed.encode("utf-8")), 4)
    total_tokens += ceil_div(len(request.user_input.encode("utf-8")), 4)
    for msg in request.history:
        msg_bytes = len(msg.role.encode("utf-8")) + len(msg.content.encode("utf-8"))
        total_tokens += PER_ITEM_OVERHEAD_TOKENS + ceil_div(msg_bytes, 4)
    for fact in request.facts:
        fact_bytes = len(fact.category.encode("utf-8")) + len(
            fact.value.encode("utf-8")
        )
        total_tokens += PER_ITEM_OVERHEAD_TOKENS + ceil_div(fact_bytes, 4)
    return total_tokens


def calculate_reservation_micro_usd(
    bound_input_tokens: int, max_output_tokens: int, profile: PriceProfile
) -> int:
    """Compute worst-case integer micro-USD reservation ceiling; fail closed if <= 0."""
    in_tokens = _require_non_bool_int(bound_input_tokens, label="bound_input_tokens")
    out_tokens = _require_non_bool_int(max_output_tokens, label="max_output_tokens")
    if in_tokens <= 0 or out_tokens <= 0:
        raise PricingError("Reservation token bounds must be positive; failing closed.")

    worst_input_rate = max(
        profile.input_micro_usd_per_1m,
        profile.cache_read_micro_usd_per_1m,
        profile.cache_write_micro_usd_per_1m,
    )
    worst_output_rate = profile.output_micro_usd_per_1m
    if (
        profile.long_context_threshold_tokens is not None
        and in_tokens > profile.long_context_threshold_tokens
    ):
        if (
            profile.long_context_input_micro_usd_per_1m is None
            or profile.long_context_output_micro_usd_per_1m is None
        ):
            raise PricingError("Long-context pricing rates are missing.")
        worst_input_rate = max(
            worst_input_rate, profile.long_context_input_micro_usd_per_1m
        )
        worst_output_rate = max(
            worst_output_rate, profile.long_context_output_micro_usd_per_1m
        )

    numerator = (in_tokens * worst_input_rate) + (out_tokens * worst_output_rate)
    reserved = ceil_div(numerator, TOKENS_PER_MILLION)
    if reserved <= 0:
        raise PricingError(
            "Computed reservation must be greater than zero micro-USD; failing closed."
        )
    return reserved


def calculate_settlement_micro_usd(
    usage: UsageMetering, frozen_profile: PriceProfile
) -> SettlementCalc:
    """Compute exact ceiling-rounded micro-USD cost from a receipt's usage metering."""
    uncached = _require_non_bool_int(
        usage.uncached_input_tokens, label="uncached_input_tokens"
    )
    cache_read = _require_non_bool_int(
        usage.cache_read_tokens, label="cache_read_tokens"
    )
    cache_write = _require_non_bool_int(
        usage.cache_write_tokens, label="cache_write_tokens"
    )
    output = _require_non_bool_int(usage.output_tokens, label="output_tokens")
    reasoning = _require_non_bool_int(usage.reasoning_tokens, label="reasoning_tokens")

    for name, val in (
        ("uncached_input_tokens", uncached),
        ("cache_read_tokens", cache_read),
        ("cache_write_tokens", cache_write),
        ("output_tokens", output),
        ("reasoning_tokens", reasoning),
    ):
        if val < 0:
            raise PricingError(f"Usage count '{name}' must be non-negative.")

    if reasoning > output:
        raise PricingError(
            f"reasoning_tokens ({reasoning}) cannot exceed output_tokens ({output})."
        )

    if usage.model_id != frozen_profile.model_id or usage.tier != frozen_profile.tier:
        raise PricingError(
            f"Receipt model/tier mismatch: got ({usage.model_id!r}, {usage.tier!r}), "
            f"expected ({frozen_profile.model_id!r}, {frozen_profile.tier!r})."
        )

    total_input = uncached + cache_read + cache_write
    long_context_applied = (
        frozen_profile.long_context_threshold_tokens is not None
        and total_input > frozen_profile.long_context_threshold_tokens
    )
    if long_context_applied:
        if (
            frozen_profile.long_context_input_micro_usd_per_1m is None
            or frozen_profile.long_context_output_micro_usd_per_1m is None
        ):
            raise PricingError("Long-context pricing rates missing in frozen profile.")
        input_rate = frozen_profile.long_context_input_micro_usd_per_1m
        output_rate = frozen_profile.long_context_output_micro_usd_per_1m
    else:
        input_rate = frozen_profile.input_micro_usd_per_1m
        output_rate = frozen_profile.output_micro_usd_per_1m

    cache_read_rate = frozen_profile.cache_read_micro_usd_per_1m
    cache_write_rate = frozen_profile.cache_write_micro_usd_per_1m

    numerator = (
        uncached * input_rate
        + cache_read * cache_read_rate
        + cache_write * cache_write_rate
        + output * output_rate
    )
    settled = ceil_div(numerator, TOKENS_PER_MILLION)

    return SettlementCalc(
        uncached_input_tokens=uncached,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output,
        reasoning_tokens=reasoning,
        total_input_tokens=total_input,
        long_context_applied=long_context_applied,
        input_rate_micro_usd_per_1m=input_rate,
        cache_read_rate_micro_usd_per_1m=cache_read_rate,
        cache_write_rate_micro_usd_per_1m=cache_write_rate,
        output_rate_micro_usd_per_1m=output_rate,
        numerator_micro_usd_tokens=numerator,
        settled_micro_usd=settled,
    )
