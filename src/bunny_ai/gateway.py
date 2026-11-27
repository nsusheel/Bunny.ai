"""Provider-neutral model gateway with deterministic pre-flight enforcement."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from bunny_ai._watermark import AttemptRecord
from bunny_ai.budget import (
    DEFAULT_PRICE_PROFILES,
    PriceProfile,
    calculate_reservation_micro_usd,
    estimate_conservative_input_tokens,
    price_profile_to_json,
    validate_price_profile,
)
from bunny_ai.errors import (
    BudgetError,
    DispatchBlockedError,
    GatewayError,
    NotDispatchedError,
    PricingError,
    TransportUncertainError,
)
from bunny_ai.ledger import (
    BudgetLedger,
    compute_consent_digest,
    compute_consent_digest_from_conn,
    compute_consent_digest_from_parts,
    compute_facts_digest,
)
from bunny_ai.providers.types import (
    ProviderDispatchRequest,
    ProviderReceipt,
    ProviderRequest,
    ProviderResponse,
    ProviderTransport,
    RequestOptions,
)
from bunny_ai.storage import LocalStorage

GatewayStatus = Literal[
    "settled", "refused", "released", "uncertain_hold", "overrun_blocked"
]


@dataclass(frozen=True)
class ConsentSnapshot:
    """Consistent snapshot of local memory consent state at request preparation."""

    identity_id: str
    memory_enabled: bool
    context_floor: str | None
    facts_digest: str


def compute_consent_snapshot(
    storage: LocalStorage, identity_id: str
) -> ConsentSnapshot:
    """Read a consistent ``ConsentSnapshot`` from ``storage``."""
    with storage.read_transaction("snapshot memory consent") as conn:
        enabled, floor, facts_digest, _ = compute_consent_digest_from_conn(
            conn, identity_id
        )
    return ConsentSnapshot(
        identity_id=identity_id,
        memory_enabled=enabled,
        context_floor=floor,
        facts_digest=facts_digest,
    )


def compute_request_digest(
    request: ProviderRequest,
    *,
    profile_id: str = "",
    model_id: str = "",
    tier: str = "",
    options: RequestOptions | None = None,
) -> str:
    """Compute a deterministic SHA-256 digest of the provider request envelope."""
    opts = options if options is not None else RequestOptions()
    payload = {
        "profile_id": profile_id,
        "model_id": model_id,
        "tier": tier,
        "request": asdict(request),
        "options": asdict(opts),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PreparedGatewayRequest:
    """Validated or pre-refused gateway dispatch plan."""

    attempt_id: str
    idempotency_key: str
    profile_id: str
    profile: PriceProfile | None
    request: ProviderRequest
    options: RequestOptions
    consent: ConsentSnapshot
    consent_digest: str
    request_digest: str
    bound_input_tokens: int
    reserved_micro_usd: int
    refusal_code: str | None = None
    refusal_reason: str | None = None


@dataclass(frozen=True)
class GatewayOutcome:
    """Result of executing one gateway dispatch."""

    status: GatewayStatus
    attempt_id: str
    idempotency_key: str
    response: ProviderResponse | None = None
    receipt: ProviderReceipt | None = None
    reserved_micro_usd: int = 0
    settled_micro_usd: int = 0
    held_micro_usd: int = 0
    overrun_micro_usd: int = 0
    refusal_code: str | None = None
    reason: str | None = None


def _validate_request_options(
    options: RequestOptions, profile: PriceProfile
) -> tuple[str | None, str | None]:
    if options.store:
        return ("unsupported_option", "Provider-side storage (store=True) is disabled.")
    if options.tools_enabled:
        return ("unsupported_option", "Tools are disabled in this Awakening build.")
    if options.streaming:
        return ("unsupported_option", "Streaming is disabled in this Awakening build.")
    if options.background:
        return ("unsupported_option", "Background execution is disabled.")
    if options.auto_truncation:
        return ("unsupported_option", "Auto-truncation is disabled.")
    if options.previous_response_id is not None:
        return ("unsupported_option", "Stateful previous_response_id is disabled.")
    if isinstance(options.max_output_tokens, bool) or not isinstance(
        options.max_output_tokens, int
    ):
        return ("invalid_output_limit", "max_output_tokens must be an integer.")
    if options.max_output_tokens < 1:
        return ("invalid_output_limit", "max_output_tokens must be at least 1.")
    if options.max_output_tokens > profile.max_output_tokens:
        return (
            "output_limit_exceeded",
            f"Requested output limit {options.max_output_tokens} exceeds profile max "
            f"{profile.max_output_tokens}.",
        )
    return (None, None)


def _validate_request_bounds(
    request: ProviderRequest, profile: PriceProfile
) -> tuple[int, str | None, str | None]:
    if not profile.text_only:
        return (0, "unsupported_profile", "Non-text-only profiles are not supported.")
    if len(request.user_input) > profile.max_input_chars:
        return (
            0,
            "input_too_large",
            f"User input length {len(request.user_input)} exceeds profile limit "
            f"{profile.max_input_chars} chars.",
        )
    try:
        bound_tokens = estimate_conservative_input_tokens(request, profile)
    except PricingError as exc:
        return (0, "pricing_incomplete", str(exc))
    if bound_tokens > profile.max_input_tokens:
        return (
            bound_tokens,
            "input_token_limit_exceeded",
            f"Conservative input token bound {bound_tokens} exceeds profile limit "
            f"{profile.max_input_tokens}.",
        )
    return (bound_tokens, None, None)


class ModelGateway:
    """Enforces pricing, consent, budget, at-most-once send, and settlement."""

    def __init__(
        self,
        storage: LocalStorage,
        ledger: BudgetLedger,
        transport: ProviderTransport,
        *,
        price_profiles: Mapping[str, PriceProfile | None] | None = None,
        clock: Callable[[], datetime] | None = None,
        before_authorize: Callable[[], None] | None = None,
        after_reserve: Callable[[str], None] | None = None,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._storage = storage
        self._ledger = ledger
        self._transport = transport
        self._price_profiles: Mapping[str, PriceProfile | None] = (
            DEFAULT_PRICE_PROFILES if price_profiles is None else price_profiles
        )
        self._clock: Callable[[], datetime] = (
            (lambda: datetime.now(UTC)) if clock is None else clock
        )
        self._before_authorize = before_authorize
        self._after_reserve = after_reserve
        self._request_timeout_seconds = float(request_timeout_seconds)

    def prepare(
        self,
        *,
        profile_id: str,
        request: ProviderRequest,
        options: RequestOptions | None = None,
        consent: ConsentSnapshot | None = None,
        idempotency_key: str | None = None,
        attempt_id: str | None = None,
    ) -> PreparedGatewayRequest:
        """Validate pricing, options, input bounds, and compute reservation ceiling."""
        aid = attempt_id if attempt_id is not None else str(uuid.uuid4())
        ikey = idempotency_key if idempotency_key is not None else str(uuid.uuid4())
        opts = options if options is not None else RequestOptions()
        snap = (
            consent
            if consent is not None
            else compute_consent_snapshot(self._storage, request.identity_id)
        )
        consent_digest = compute_consent_digest_from_parts(
            snap.memory_enabled, snap.context_floor, snap.facts_digest
        )

        def _refuse(
            code: str,
            reason: str,
            *,
            profile: PriceProfile | None = None,
            bound: int = 0,
        ) -> PreparedGatewayRequest:
            return PreparedGatewayRequest(
                attempt_id=aid,
                idempotency_key=ikey,
                profile_id=profile_id,
                profile=profile,
                request=request,
                options=opts,
                consent=snap,
                consent_digest=consent_digest,
                request_digest=compute_request_digest(
                    request,
                    profile_id=profile_id,
                    model_id=profile.model_id if profile is not None else "",
                    tier=profile.tier if profile is not None else "",
                    options=opts,
                ),
                bound_input_tokens=bound,
                reserved_micro_usd=0,
                refusal_code=code,
                refusal_reason=reason,
            )

        if profile_id not in self._price_profiles:
            return _refuse(
                "pricing_missing",
                f"Pricing profile '{profile_id}' is not configured.",
            )
        profile = self._price_profiles[profile_id]
        if profile is None:
            return _refuse(
                "pricing_incomplete",
                f"Pricing profile '{profile_id}' is incomplete or malformed.",
            )

        price_err, price_reason = validate_price_profile(profile, self._clock())
        if price_err is not None:
            return _refuse(
                price_err,
                price_reason or "Pricing validation failed.",
                profile=profile,
            )

        opt_err, opt_reason = _validate_request_options(opts, profile)
        if opt_err is not None:
            return _refuse(
                opt_err,
                opt_reason or "Request option rejected.",
                profile=profile,
            )

        bound_tokens, bound_err, bound_reason = _validate_request_bounds(
            request, profile
        )
        if bound_err is not None:
            return _refuse(
                bound_err,
                bound_reason or "Request bounds rejected.",
                profile=profile,
                bound=bound_tokens,
            )

        try:
            reserved = calculate_reservation_micro_usd(
                bound_tokens, opts.max_output_tokens, profile
            )
        except PricingError as exc:
            return _refuse(
                "pricing_incomplete", str(exc), profile=profile, bound=bound_tokens
            )

        return PreparedGatewayRequest(
            attempt_id=aid,
            idempotency_key=ikey,
            profile_id=profile_id,
            profile=profile,
            request=request,
            options=opts,
            consent=snap,
            consent_digest=consent_digest,
            request_digest=compute_request_digest(
                request,
                profile_id=profile_id,
                model_id=profile.model_id,
                tier=profile.tier,
                options=opts,
            ),
            bound_input_tokens=bound_tokens,
            reserved_micro_usd=reserved,
        )

    def _record_and_return_refusal(
        self,
        prepared: PreparedGatewayRequest,
        *,
        refusal_code: str,
        reason: str,
        now_utc: datetime,
    ) -> GatewayOutcome:
        profile = prepared.profile
        self._ledger.record_refusal(
            attempt_id=prepared.attempt_id,
            idempotency_key=prepared.idempotency_key,
            profile_id=prepared.profile_id,
            provider_id=profile.provider_id if profile is not None else "unknown",
            model_id=profile.model_id if profile is not None else "unknown",
            tier=profile.tier if profile is not None else "unknown",
            price_version=profile.price_version if profile is not None else "unknown",
            frozen_price_json=(
                price_profile_to_json(profile) if profile is not None else "{}"
            ),
            request_digest=prepared.request_digest,
            consent_digest=prepared.consent_digest,
            refusal_code=refusal_code,
            reason=reason,
            now_utc=now_utc,
        )
        return GatewayOutcome(
            status="refused",
            attempt_id=prepared.attempt_id,
            idempotency_key=prepared.idempotency_key,
            refusal_code=refusal_code,
            reason=reason,
        )

    def dispatch(self, prepared: PreparedGatewayRequest) -> GatewayOutcome:
        """Authorize, reserve, execute transport at most once, and settle."""
        now_utc = self._clock()
        if prepared.refusal_code is not None or prepared.profile is None:
            return self._record_and_return_refusal(
                prepared,
                refusal_code=prepared.refusal_code or "pricing_missing",
                reason=prepared.refusal_reason
                or "Request failed pre-flight validation.",
                now_utc=now_utc,
            )

        price_err, price_reason = validate_price_profile(prepared.profile, now_utc)
        if price_err is not None:
            return self._record_and_return_refusal(
                prepared,
                refusal_code=price_err,
                reason=price_reason or "Pricing validation failed.",
                now_utc=now_utc,
            )

        if self._before_authorize is not None:
            self._before_authorize()

        try:
            self._ledger.reserve_attempt(
                attempt_id=prepared.attempt_id,
                idempotency_key=prepared.idempotency_key,
                profile=prepared.profile,
                request_digest=prepared.request_digest,
                expected_consent_digest=prepared.consent_digest,
                reserved_micro_usd=prepared.reserved_micro_usd,
                now_utc=self._clock(),
            )
        except (DispatchBlockedError, BudgetError, PricingError, GatewayError) as exc:
            return self._record_and_return_refusal(
                prepared,
                refusal_code=exc.refusal_code or "budget_refused",
                reason=str(exc),
                now_utc=self._clock(),
            )

        if self._after_reserve is not None:
            self._after_reserve(prepared.attempt_id)

        dispatch_request = ProviderDispatchRequest(
            attempt_id=prepared.attempt_id,
            idempotency_key=prepared.idempotency_key,
            provider_id=prepared.profile.provider_id,
            model_id=prepared.profile.model_id,
            tier=prepared.profile.tier,
            endpoint=prepared.profile.endpoint,
            request=prepared.request,
            options=prepared.options,
            deadline_seconds=self._request_timeout_seconds,
        )

        try:
            with self._ledger.dispatch_lock():
                receipt = self._transport.send(dispatch_request)
        except NotDispatchedError as exc:
            released = self._ledger.release_not_dispatched(
                prepared.attempt_id,
                reason=str(exc) or "not_dispatched",
                now_utc=self._clock(),
            )
            return GatewayOutcome(
                status="released",
                attempt_id=prepared.attempt_id,
                idempotency_key=prepared.idempotency_key,
                reserved_micro_usd=released.reserved_micro_usd,
                held_micro_usd=0,
                settled_micro_usd=0,
                refusal_code="not_dispatched",
                reason=released.reason,
            )
        except (TransportUncertainError, Exception) as exc:
            held = self._ledger.mark_uncertain_hold(
                prepared.attempt_id,
                reason=str(exc) or exc.__class__.__name__,
                now_utc=self._clock(),
            )
            return GatewayOutcome(
                status="uncertain_hold",
                attempt_id=prepared.attempt_id,
                idempotency_key=prepared.idempotency_key,
                reserved_micro_usd=held.reserved_micro_usd,
                held_micro_usd=held.held_micro_usd,
                settled_micro_usd=0,
                reason=held.reason,
            )

        if receipt.attempt_id != prepared.attempt_id:
            held = self._ledger.mark_uncertain_hold(
                prepared.attempt_id,
                reason=(
                    f"Receipt attempt_id mismatch: expected {prepared.attempt_id!r}, "
                    f"got {receipt.attempt_id!r}."
                ),
                now_utc=self._clock(),
            )
            return GatewayOutcome(
                status="uncertain_hold",
                attempt_id=prepared.attempt_id,
                idempotency_key=prepared.idempotency_key,
                receipt=receipt,
                reserved_micro_usd=held.reserved_micro_usd,
                held_micro_usd=held.held_micro_usd,
                settled_micro_usd=0,
                reason=held.reason,
            )

        if (
            receipt.provider_error is not None
            or not receipt.complete
            or receipt.usage is None
        ):
            held = self._ledger.mark_uncertain_hold(
                prepared.attempt_id,
                reason=receipt.provider_error
                or f"incomplete_receipt:{receipt.finish_reason}",
                now_utc=self._clock(),
            )
            return GatewayOutcome(
                status="uncertain_hold",
                attempt_id=prepared.attempt_id,
                idempotency_key=prepared.idempotency_key,
                receipt=receipt,
                reserved_micro_usd=held.reserved_micro_usd,
                held_micro_usd=held.held_micro_usd,
                settled_micro_usd=0,
                reason=held.reason,
            )

        settled = self._ledger.settle_receipt(
            prepared.attempt_id, receipt, now_utc=self._clock()
        )
        if settled.state == "overrun_blocked":
            return GatewayOutcome(
                status="overrun_blocked",
                attempt_id=prepared.attempt_id,
                idempotency_key=prepared.idempotency_key,
                response=receipt.response,
                receipt=receipt,
                reserved_micro_usd=settled.reserved_micro_usd,
                settled_micro_usd=settled.settled_micro_usd,
                held_micro_usd=settled.held_micro_usd,
                overrun_micro_usd=settled.overrun_micro_usd,
                reason=settled.reason,
            )

        return GatewayOutcome(
            status="settled",
            attempt_id=prepared.attempt_id,
            idempotency_key=prepared.idempotency_key,
            response=receipt.response,
            receipt=receipt,
            reserved_micro_usd=settled.reserved_micro_usd,
            settled_micro_usd=settled.settled_micro_usd,
            held_micro_usd=0,
            overrun_micro_usd=0,
        )

    def settle_receipt(
        self, attempt_id: str, receipt: ProviderReceipt
    ) -> AttemptRecord:
        """Directly settle a receipt idempotently through the underlying ledger."""
        return self._ledger.settle_receipt(attempt_id, receipt, now_utc=self._clock())


__all__ = [
    "ConsentSnapshot",
    "GatewayOutcome",
    "GatewayStatus",
    "ModelGateway",
    "PreparedGatewayRequest",
    "compute_consent_digest",
    "compute_consent_snapshot",
    "compute_facts_digest",
    "compute_request_digest",
]
