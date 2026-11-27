"""Unit tests for Awakening Stage 4 gateway, integer micro-USD budget, and ledger."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bunny_ai.app import AwakeningSession
from bunny_ai.budget import (
    DEFAULT_PRICE_PROFILES,
    DEFAULT_PROFILE_ID,
    PriceProfile,
    calculate_reservation_micro_usd,
    calculate_settlement_micro_usd,
    ceil_div,
    estimate_conservative_input_tokens,
    format_micro_usd,
    load_pricing_file,
    price_profile_from_json,
    price_profile_to_json,
    validate_price_profile,
)
from bunny_ai.cli import main as cli_main
from bunny_ai.config import (
    AppConfig,
    BudgetSettings,
    GatewaySettings,
    HistorySettings,
    load_config,
)
from bunny_ai.errors import (
    ConfigError,
    NotDispatchedError,
    PricingError,
    TransportUncertainError,
)
from bunny_ai.gateway import ModelGateway
from bunny_ai.ledger import BudgetLedger
from bunny_ai.memory import MemoryStore
from bunny_ai.migrations import (
    CURRENT_SCHEMA_VERSION,
    m001_identity,
    m002_history,
    m003_memory,
)
from bunny_ai.providers import (
    FakeProvider,
    ProviderDispatchRequest,
    ProviderReceipt,
    ProviderRequest,
    ProviderResponse,
    RequestOptions,
    SimulatedTransport,
    TurnMessage,
    UsageMetering,
)
from bunny_ai.storage import DB_FILENAME, LocalStorage

FIXED_NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)


def _sample_request(
    identity_id: str = "11111111-2222-4333-8444-555555555555",
    user_input: str = "Hello Bunny",
    history: tuple[TurnMessage, ...] = (),
) -> ProviderRequest:
    return ProviderRequest(
        identity_id=identity_id,
        seed_version="seed-1",
        style_version="style-1",
        system_seed="Neon seed seed-1 (style-1).",
        history=history,
        user_input=user_input,
        facts=(),
    )


def _open_ledger(tmp_path: Path) -> tuple[LocalStorage, BudgetLedger, str]:
    storage = LocalStorage(tmp_path / "profile")
    init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    ledger = BudgetLedger(storage, init.identity.identity_id)
    ledger.check_and_reconcile_watermark(FIXED_NOW)
    return storage, ledger, init.identity.identity_id


class _ScriptedTransport:
    """Minimal test transport whose next outcome can be scripted."""

    def __init__(
        self, handler: Callable[[ProviderDispatchRequest], ProviderReceipt]
    ) -> None:
        self._handler = handler
        self.call_count = 0
        self.requests: list[ProviderDispatchRequest] = []

    def send(self, dispatch_request: ProviderDispatchRequest) -> ProviderReceipt:
        self.call_count += 1
        self.requests.append(dispatch_request)
        return self._handler(dispatch_request)


def test_integer_math_and_formatting() -> None:
    assert ceil_div(0, 1_000_000) == 0
    assert ceil_div(1, 1_000_000) == 1
    assert ceil_div(1_000_000, 1_000_000) == 1
    assert ceil_div(1_000_001, 1_000_000) == 2

    with pytest.raises(ValueError):
        ceil_div(-1, 10)
    with pytest.raises(ValueError):
        ceil_div(10, 0)
    with pytest.raises(PricingError):
        ceil_div(True, 10)

    assert format_micro_usd(0) == "$0.000000"
    assert format_micro_usd(563) == "$0.000563"
    assert format_micro_usd(50_000) == "$0.050000"
    assert format_micro_usd(-1_000) == "-$0.001000"
    assert format_micro_usd(2_500_001) == "$2.500001"


def test_price_profile_validation_json_and_file_loading(tmp_path: Path) -> None:
    default = DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID]
    assert validate_price_profile(default, FIXED_NOW) == (None, None)
    assert validate_price_profile(None, FIXED_NOW)[0] == "pricing_missing"

    before_start = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert validate_price_profile(default, before_start)[0] == "pricing_expired"

    exact_end = datetime(2027, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert validate_price_profile(default, exact_end)[0] == "pricing_expired"

    zero_rates = replace(
        default,
        input_micro_usd_per_1m=0,
        cache_read_micro_usd_per_1m=0,
        cache_write_micro_usd_per_1m=0,
        output_micro_usd_per_1m=0,
    )
    assert validate_price_profile(zero_rates, FIXED_NOW)[0] == "pricing_incomplete"

    bad_window = replace(
        default,
        valid_from_utc="2027-01-01T00:00:00Z",
        valid_until_utc="2026-01-01T00:00:00Z",
    )
    assert validate_price_profile(bad_window, FIXED_NOW)[0] == "pricing_incomplete"

    serialized = price_profile_to_json(default)
    round_tripped = price_profile_from_json(serialized)
    assert round_tripped == default

    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "sim-default": json.loads(serialized),
                    "broken": {"provider_id": "simulated"},
                }
            }
        ),
        encoding="utf-8",
    )
    loaded = load_pricing_file(pricing_path)
    assert loaded["sim-default"] == default
    assert loaded["broken"] is None


def test_reservation_and_settlement_math() -> None:
    profile = replace(
        DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID],
        long_context_threshold_tokens=1_000,
        long_context_input_micro_usd_per_1m=200_000,
        long_context_output_micro_usd_per_1m=800_000,
    )
    req = _sample_request()
    bound_env = estimate_conservative_input_tokens(req, profile)
    assert 256 < bound_env < 1_000

    ceiling_profile = replace(profile, input_bound_mode="context_ceiling")
    assert estimate_conservative_input_tokens(req, ceiling_profile) == 4_096

    res_normal = calculate_reservation_micro_usd(300, 1_024, profile)
    # worst input = 125_000 (cache_write), output = 500_000:
    # (300 * 125_000 + 1024 * 500_000) / 1_000_000 = 549.5 -> 550
    assert res_normal == 550

    res_long = calculate_reservation_micro_usd(1_500, 1_024, profile)
    # worst input = 200_000, output = 800_000:
    # (1500 * 200_000 + 1024 * 800_000) / 1_000_000 = 1119.2 -> 1120
    assert res_long == 1_120

    usage = UsageMetering(
        uncached_input_tokens=100,
        cache_read_tokens=200,
        cache_write_tokens=50,
        output_tokens=40,
        reasoning_tokens=10,
        model_id=profile.model_id,
        tier=profile.tier,
    )
    calc = calculate_settlement_micro_usd(usage, profile)
    # (100*100_000 + 200*10_000 + 50*125_000 + 40*500_000) = 38_250_000 -> ceil = 39
    assert calc.settled_micro_usd == 39
    assert calc.long_context_applied is False

    long_usage = UsageMetering(
        uncached_input_tokens=900,
        cache_read_tokens=200,
        cache_write_tokens=0,
        output_tokens=10,
        reasoning_tokens=0,
        model_id=profile.model_id,
        tier=profile.tier,
    )
    long_calc = calculate_settlement_micro_usd(long_usage, profile)
    assert long_calc.long_context_applied is True
    assert long_calc.settled_micro_usd == ceil_div(
        900 * 200_000 + 200 * 10_000 + 10 * 800_000, 1_000_000
    )

    with pytest.raises(PricingError, match="mismatch"):
        calculate_settlement_micro_usd(replace(usage, model_id="other"), profile)
    with pytest.raises(PricingError, match="reasoning_tokens"):
        calculate_settlement_micro_usd(replace(usage, reasoning_tokens=41), profile)
    with pytest.raises(PricingError, match="non-negative"):
        calculate_settlement_micro_usd(replace(usage, output_tokens=-1), profile)


def test_simulated_transport_deterministic_metering() -> None:
    transport = SimulatedTransport(FakeProvider())
    req = _sample_request()
    dispatch_req = ProviderDispatchRequest(
        attempt_id="att-1",
        idempotency_key="idem-1",
        provider_id="simulated",
        model_id="sim-neon-1",
        tier="standard",
        endpoint="simulated://local/v1/responses",
        request=req,
        options=RequestOptions(max_output_tokens=12),
        deadline_seconds=30.0,
    )
    receipt = transport.send(dispatch_req)
    assert transport.call_count == 1
    assert len(transport.dispatched_requests) == 1
    assert receipt.complete is True
    assert receipt.response is not None
    assert receipt.usage is not None
    assert receipt.usage.model_id == "sim-neon-1"
    assert receipt.usage.tier == "standard"
    assert receipt.usage.output_tokens <= 12


def test_config_gateway_and_budget_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BUNNY_CONFIG", raising=False)
    monkeypatch.delenv("BUNNY_DATA_DIR", raising=False)
    cfg_file = tmp_path / "bunny.toml"
    cfg_file.write_text(
        """
[gateway]
profile = "sim-default"
output_limit = 512
request_timeout_seconds = 15
pricing_file = "pricing.json"

[budget]
per_request_micro_usd = 25000
cumulative_micro_usd = 100000
""".strip(),
        encoding="utf-8",
    )
    cfg = load_config(cli_config_path=cfg_file, cli_data_dir=tmp_path / "data")
    assert cfg.gateway.profile == "sim-default"
    assert cfg.gateway.output_limit == 512
    assert cfg.gateway.request_timeout_seconds == 15
    assert cfg.gateway.pricing_file == (tmp_path / "pricing.json").resolve()
    assert cfg.budget.per_request_micro_usd == 25_000
    assert cfg.budget.cumulative_micro_usd == 100_000

    for bad_toml in (
        "[gateway]\nmax_output_tokens = 100\n",
        "[gateway]\noutput_limit = 0\n",
        "[gateway]\nrequest_timeout_seconds = 301\n",
        "[budget]\nper_request_micro_usd = -1\n",
        "[budget]\ncumulative_micro_usd = true\n",
    ):
        bad_file = tmp_path / "bad.toml"
        bad_file.write_text(bad_toml, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(cli_config_path=bad_file, cli_data_dir=tmp_path / "data")


def test_gateway_refusals_never_call_transport(tmp_path: Path) -> None:
    storage, ledger, identity_id = _open_ledger(tmp_path)
    try:
        transport = SimulatedTransport()
        expired_profile = replace(
            DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID],
            valid_until_utc="2026-01-02T00:00:00Z",
        )
        profiles: dict[str, PriceProfile | None] = {
            "sim-default": DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID],
            "broken": None,
            "expired": expired_profile,
        }
        gw = ModelGateway(
            storage,
            ledger,
            transport,
            price_profiles=profiles,
            clock=lambda: FIXED_NOW,
        )
        req = _sample_request(identity_id)

        for profile_id, expected_code in (
            ("missing", "pricing_missing"),
            ("broken", "pricing_incomplete"),
            ("expired", "pricing_expired"),
        ):
            outcome = gw.dispatch(gw.prepare(profile_id=profile_id, request=req))
            assert outcome.status == "refused"
            assert outcome.refusal_code == expected_code

        for bad_opts in (
            RequestOptions(store=True),
            RequestOptions(tools_enabled=True),
            RequestOptions(streaming=True),
            RequestOptions(background=True),
            RequestOptions(auto_truncation=True),
            RequestOptions(previous_response_id="resp_1"),
            RequestOptions(max_output_tokens=2048),
        ):
            outcome = gw.dispatch(
                gw.prepare(profile_id="sim-default", request=req, options=bad_opts)
            )
            assert outcome.status == "refused"

        ledger.set_allowances(per_request_micro_usd=100, now_utc=FIXED_NOW)
        per_req = gw.dispatch(gw.prepare(profile_id="sim-default", request=req))
        assert per_req.status == "refused"
        assert per_req.refusal_code == "per_request_budget_exceeded"

        ledger.set_allowances(
            per_request_micro_usd=50_000, cumulative_micro_usd=100, now_utc=FIXED_NOW
        )
        cum_req = gw.dispatch(gw.prepare(profile_id="sim-default", request=req))
        assert cum_req.status == "refused"
        assert cum_req.refusal_code == "cumulative_budget_exceeded"

        assert transport.call_count == 0
    finally:
        storage.close()


def test_gateway_refuses_consent_drift_and_duplicate_idempotency_key(
    tmp_path: Path,
) -> None:
    storage, ledger, identity_id = _open_ledger(tmp_path)
    try:
        transport = SimulatedTransport()
        memory = MemoryStore(storage, identity_id)

        def mutate_consent() -> None:
            memory.set_enabled(False)

        gw_drift = ModelGateway(
            storage,
            ledger,
            transport,
            clock=lambda: FIXED_NOW,
            before_authorize=mutate_consent,
        )
        req = _sample_request(identity_id)
        drift_outcome = gw_drift.dispatch(
            gw_drift.prepare(profile_id="sim-default", request=req)
        )
        assert drift_outcome.status == "refused"
        assert drift_outcome.refusal_code == "consent_changed"
        assert transport.call_count == 0

        gw_ok = ModelGateway(storage, ledger, transport, clock=lambda: FIXED_NOW)
        first = gw_ok.dispatch(
            gw_ok.prepare(
                profile_id="sim-default", request=req, idempotency_key="fixed-key"
            )
        )
        assert first.status == "settled"
        assert transport.call_count == 1

        dup = gw_ok.dispatch(
            gw_ok.prepare(
                profile_id="sim-default", request=req, idempotency_key="fixed-key"
            )
        )
        assert dup.status == "refused"
        assert dup.refusal_code == "duplicate_idempotency_key"
        assert transport.call_count == 1
    finally:
        storage.close()


def test_not_dispatched_releases_and_uncertain_holds_persist(tmp_path: Path) -> None:
    storage, ledger, identity_id = _open_ledger(tmp_path)
    try:

        def raise_not_dispatched(_req: ProviderDispatchRequest) -> ProviderReceipt:
            raise NotDispatchedError("connection refused locally")

        t1 = _ScriptedTransport(raise_not_dispatched)
        gw1 = ModelGateway(storage, ledger, t1, clock=lambda: FIXED_NOW)
        req = _sample_request(identity_id)
        out1 = gw1.dispatch(gw1.prepare(profile_id="sim-default", request=req))
        assert out1.status == "released"
        assert ledger.summary().held_micro_usd == 0
        assert ledger.summary().settled_micro_usd == 0

        def raise_uncertain(_req: ProviderDispatchRequest) -> ProviderReceipt:
            raise TransportUncertainError("socket timeout after send")

        t2 = _ScriptedTransport(raise_uncertain)
        gw2 = ModelGateway(storage, ledger, t2, clock=lambda: FIXED_NOW)
        out2 = gw2.dispatch(gw2.prepare(profile_id="sim-default", request=req))
        assert out2.status == "uncertain_hold"
        assert out2.held_micro_usd > 0
    finally:
        storage.close()

    # Reopen across restart: uncertain hold must remain held.
    storage2 = LocalStorage(tmp_path / "profile")
    storage2.initialize_or_load(seed_version="seed-1", style_version="style-1")
    try:
        ledger2 = BudgetLedger(storage2, identity_id)
        summary = ledger2.summary()
        assert summary.uncertain_hold_count == 1
        assert summary.held_micro_usd == out2.held_micro_usd

        reconciled = ledger2.reconcile_uncertain_hold(
            out2.attempt_id, action="settle", settled_micro_usd=25, now_utc=FIXED_NOW
        )
        assert reconciled.state == "settled"
        assert reconciled.settled_micro_usd == 25
        assert ledger2.summary().held_micro_usd == 0
    finally:
        storage2.close()


def test_crash_recovery_promotes_orphaned_reserved_unless_dispatch_locked(
    tmp_path: Path,
) -> None:
    storage, ledger, identity_id = _open_ledger(tmp_path)
    try:

        def simulate_crash(_attempt_id: str) -> None:
            raise RuntimeError("simulated hard crash after reserve")

        gw = ModelGateway(
            storage,
            ledger,
            SimulatedTransport(),
            clock=lambda: FIXED_NOW,
            after_reserve=simulate_crash,
        )
        with pytest.raises(RuntimeError, match="simulated hard crash"):
            gw.dispatch(
                gw.prepare(
                    profile_id="sim-default", request=_sample_request(identity_id)
                )
            )

        # If another dispatcher holds the shared dispatch lock, recovery leaves it alone.
        with ledger.dispatch_lock():
            assert ledger.recover_stale_intents(FIXED_NOW) == 0
        assert ledger.list_attempts(limit=1)[0].state == "reserved"

        # Once unlocked, crash recovery promotes orphaned reserved -> uncertain_hold.
        assert ledger.recover_stale_intents(FIXED_NOW) == 1
        att = ledger.list_attempts(limit=1)[0]
        assert att.state == "uncertain_hold"
        assert att.reason == "recovered_after_crash"
    finally:
        storage.close()


def test_idempotent_settlement_overrun_and_model_mismatch_block(tmp_path: Path) -> None:
    storage, ledger, identity_id = _open_ledger(tmp_path)
    try:
        profile = DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID]
        transport = SimulatedTransport()
        gw = ModelGateway(storage, ledger, transport, clock=lambda: FIXED_NOW)
        req = _sample_request(identity_id)

        # Normal settlement + duplicate receipt_id idempotency.
        out = gw.dispatch(gw.prepare(profile_id="sim-default", request=req))
        assert out.status == "settled"
        assert out.receipt is not None
        again = gw.settle_receipt(out.attempt_id, out.receipt)
        assert again.settled_micro_usd == out.settled_micro_usd
        assert ledger.summary().settled_micro_usd == out.settled_micro_usd

        # Overrun receipt triggers overrun_blocked + dispatch_blocked.
        def overrun_send(dreq: ProviderDispatchRequest) -> ProviderReceipt:
            return ProviderReceipt(
                receipt_id="rcpt-overrun",
                attempt_id=dreq.attempt_id,
                response=ProviderResponse(text="big", mode="sim", history_turns_used=0),
                usage=UsageMetering(
                    uncached_input_tokens=500,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    output_tokens=10_000,
                    model_id=dreq.model_id,
                    tier=dreq.tier,
                ),
            )

        gw_over = ModelGateway(
            storage, ledger, _ScriptedTransport(overrun_send), clock=lambda: FIXED_NOW
        )
        over_out = gw_over.dispatch(
            gw_over.prepare(profile_id="sim-default", request=req)
        )
        assert over_out.status == "overrun_blocked"
        assert over_out.overrun_micro_usd > 0
        assert ledger.summary().dispatch_blocked is True

        # Subsequent dispatch is blocked with 0 transport calls.
        blocked_out = gw.dispatch(gw.prepare(profile_id="sim-default", request=req))
        assert blocked_out.status == "refused"
        assert blocked_out.refusal_code == "budget_overrun"
        assert transport.call_count == 1

        ledger.clear_dispatch_block(now_utc=FIXED_NOW)
        assert ledger.summary().dispatch_blocked is False

        # Model/tier mismatch also blocks dispatch.
        def mismatch_send(dreq: ProviderDispatchRequest) -> ProviderReceipt:
            return ProviderReceipt(
                receipt_id="rcpt-mismatch",
                attempt_id=dreq.attempt_id,
                response=ProviderResponse(text="ok", mode="sim", history_turns_used=0),
                usage=UsageMetering(
                    uncached_input_tokens=10,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    output_tokens=10,
                    model_id="wrong-model",
                    tier=profile.tier,
                ),
            )

        gw_mis = ModelGateway(
            storage, ledger, _ScriptedTransport(mismatch_send), clock=lambda: FIXED_NOW
        )
        mis_out = gw_mis.dispatch(gw_mis.prepare(profile_id="sim-default", request=req))
        assert mis_out.status == "overrun_blocked"
        assert ledger.summary().dispatch_blocked is True
    finally:
        storage.close()


def test_stale_backup_anti_rollback_and_manual_file_replacement(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    storage = LocalStorage(profile_dir)
    init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    identity_id = init.identity.identity_id
    backup_empty = storage.create_backup("before-spend")
    ledger = BudgetLedger(storage, identity_id)
    ledger.check_and_reconcile_watermark(FIXED_NOW)
    gw = ModelGateway(storage, ledger, SimulatedTransport(), clock=lambda: FIXED_NOW)
    first = gw.dispatch(
        gw.prepare(
            profile_id="sim-default",
            request=_sample_request(identity_id),
            idempotency_key="spend-1",
        )
    )
    assert first.status == "settled"
    storage.close()

    # 1. Restoring stale backup via LocalStorage carries forward attempt and blocks dispatch.
    restore_res = LocalStorage(profile_dir).restore_from_backup(backup_empty)
    assert restore_res.dispatch_blocked is True
    assert restore_res.ledger_reconciled_attempts == 1

    storage_after = LocalStorage(profile_dir)
    storage_after.initialize_or_load(seed_version="seed-1", style_version="style-1")
    try:
        ledger_after = BudgetLedger(storage_after, identity_id)
        summary = ledger_after.summary()
        assert summary.dispatch_blocked is True
        assert summary.settled_micro_usd == first.settled_micro_usd
        ledger_after.clear_dispatch_block(now_utc=FIXED_NOW)
    finally:
        storage_after.close()

    # 2. Manual replacement of bunny.sqlite3 with stale backup is caught by .bunny.ledger.json.
    shutil.copy2(backup_empty, profile_dir / DB_FILENAME)
    snap = LocalStorage(profile_dir).inspect()
    assert snap.dispatch_blocked is True
    assert snap.settled_micro_usd == first.settled_micro_usd

    storage_manual = LocalStorage(profile_dir)
    storage_manual.initialize_or_load(seed_version="seed-1", style_version="style-1")
    try:
        t = SimulatedTransport()
        ledger_manual = BudgetLedger(storage_manual, identity_id)
        gw_manual = ModelGateway(
            storage_manual, ledger_manual, t, clock=lambda: FIXED_NOW
        )
        refused = gw_manual.dispatch(
            gw_manual.prepare(
                profile_id="sim-default", request=_sample_request(identity_id)
            )
        )
        assert refused.status == "refused"
        assert refused.refusal_code == "stale_backup_detected_reconcile_required"
        assert t.call_count == 0
    finally:
        storage_manual.close()


def test_v3_backup_restore_blocks_on_migration_and_reconcile(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    v3_backup = tmp_path / "v3-backup.sqlite3"
    identity_id = "99999999-1111-4222-8333-444444444444"

    conn = sqlite3.connect(str(v3_backup))
    try:
        m001_identity.apply(conn)
        m002_history.apply(conn)
        m003_memory.apply(conn)
        for ver in (1, 2, 3):
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-11-24T00:00:00Z', 'v')",
                (ver,),
            )
        conn.execute(
            "INSERT INTO identity VALUES (1, ?, 'seed-1', 'style-1', "
            "'2026-11-24T00:00:00Z', '2026-11-24T00:00:00Z', 'skipped', '2026-11-24T00:00:00Z')",
            (identity_id,),
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    # Seed profile from v3, upgrade to v4, perform spend, then restore v3 backup.
    profile_dir.mkdir()
    shutil.copy2(v3_backup, profile_dir / DB_FILENAME)
    storage = LocalStorage(profile_dir)
    init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    assert init.schema_version == CURRENT_SCHEMA_VERSION
    ledger = BudgetLedger(storage, identity_id)
    ledger.check_and_reconcile_watermark(FIXED_NOW)
    gw = ModelGateway(storage, ledger, SimulatedTransport(), clock=lambda: FIXED_NOW)
    spent = gw.dispatch(
        gw.prepare(profile_id="sim-default", request=_sample_request(identity_id))
    )
    assert spent.status == "settled"
    storage.close()

    res = LocalStorage(profile_dir).restore_from_backup(v3_backup)
    assert res.schema_version == 3
    assert res.dispatch_blocked is True

    reopened = LocalStorage(profile_dir)
    reopened.initialize_or_load(seed_version="seed-1", style_version="style-1")
    try:
        reopened_ledger = BudgetLedger(reopened, identity_id)
        assert reopened_ledger.check_and_reconcile_watermark(FIXED_NOW) is True
        summary = reopened_ledger.summary()
        assert summary.dispatch_blocked is True
        assert summary.settled_micro_usd == spent.settled_micro_usd
    finally:
        reopened.close()


def test_awakening_session_skips_history_persistence_on_refusal(tmp_path: Path) -> None:
    cfg = AppConfig(
        data_dir=tmp_path / "session-profile",
        config_path=None,
        history=HistorySettings(enabled=True),
        gateway=GatewaySettings(profile="sim-default", output_limit=256),
        budget=BudgetSettings(per_request_micro_usd=10, cumulative_micro_usd=500_000),
    )
    with AwakeningSession(cfg) as session:
        outcome = session.handle_turn("Should be refused")
        assert outcome.status == "refused"
        assert outcome.persisted is False
        assert session.history_counts() == (0, 0)


def test_missing_allowance_fails_closed_with_zero_transport_calls(
    tmp_path: Path,
) -> None:
    storage, ledger, identity_id = _open_ledger(tmp_path)
    try:
        transport = SimulatedTransport()
        gw = ModelGateway(storage, ledger, transport, clock=lambda: FIXED_NOW)
        req = _sample_request(identity_id)

        for column in ("per_request_micro_usd", "cumulative_micro_usd"):
            with storage.transaction("nullify allowance") as conn:
                conn.execute(
                    "UPDATE budget_policy SET per_request_micro_usd = 50000, "
                    "cumulative_micro_usd = 500000"
                )
                conn.execute(f"UPDATE budget_policy SET {column} = NULL")
            outcome = gw.dispatch(gw.prepare(profile_id="sim-default", request=req))
            assert outcome.status == "refused"
            assert outcome.refusal_code == "allowance_missing"
            assert transport.call_count == 0
    finally:
        storage.close()


def test_cli_budget_parent_flags_before_subcommand(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    profile_dir = tmp_path / "cli-budget-parent-flags"
    assert (
        cli_main(["chat", "--data-dir", str(profile_dir), "--skip-intro", "-m", "hi"])
        == 0
    )
    capsys.readouterr()

    assert (
        cli_main(
            [
                "budget",
                "--data-dir",
                str(profile_dir),
                "set",
                "--per-request",
                "12345",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "$0.012345" in out

    storage = LocalStorage(profile_dir)
    init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    try:
        assert (
            BudgetLedger(storage, init.identity.identity_id)
            .summary()
            .per_request_micro_usd
            == 12_345
        )
    finally:
        storage.close()


def test_dispatch_same_prepared_request_twice_refuses_cleanly(tmp_path: Path) -> None:
    storage, ledger, identity_id = _open_ledger(tmp_path)
    try:
        transport = SimulatedTransport()
        gw = ModelGateway(storage, ledger, transport, clock=lambda: FIXED_NOW)
        prepared = gw.prepare(
            profile_id="sim-default", request=_sample_request(identity_id)
        )

        first = gw.dispatch(prepared)
        assert first.status == "settled"
        assert transport.call_count == 1

        second = gw.dispatch(prepared)
        assert second.status == "refused"
        assert second.refusal_code == "duplicate_idempotency_key"
        assert transport.call_count == 1
    finally:
        storage.close()
