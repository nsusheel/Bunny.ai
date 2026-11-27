"""Independent Stage 4 acceptance test suite for Awakening gateway, budget, and ledger.

Every test uses synthetic profiles/prices, blocks network sockets, and strictly
verifies both ledger balances and independent fake transport call counts.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bunny_ai.app import AwakeningSession
from bunny_ai.budget import (
    DEFAULT_PRICE_PROFILES,
    DEFAULT_PROFILE_ID,
    PriceProfile,
    calculate_reservation_micro_usd,
    ceil_div,
    estimate_conservative_input_tokens,
    format_micro_usd,
    load_pricing_file,
)
from bunny_ai.config import AppConfig, GatewaySettings, load_config
from bunny_ai.errors import GatewayError
from bunny_ai.gateway import ModelGateway, compute_consent_snapshot
from bunny_ai.ledger import BudgetLedger
from bunny_ai.memory import MemoryStore
from bunny_ai.neon import SeedPersonality
from bunny_ai.providers import (
    ContextFact,
    FakeProvider,
    ModelProvider,
    ProviderDispatchRequest,
    ProviderReceipt,
    ProviderRequest,
    ProviderResponse,
    RequestOptions,
    UsageMetering,
)
from bunny_ai.storage import DB_FILENAME, LocalStorage, acquire_exclusive_file_lock

FIXED_NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
SYNTHETIC_IDENTITY_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

NETWORK_GUARD = """\
import socket


def denied(*args, **kwargs):
    raise RuntimeError("Bunny acceptance test blocked real network access")


socket.socket = denied
socket.getaddrinfo = denied
"""


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class RecordedDispatch:
    """Single transport dispatch call captured by an independent fake transport."""

    attempt_id: str
    idempotency_key: str
    model_id: str
    tier: str
    timestamp: str


def _default_usage(
    dispatch_request: ProviderDispatchRequest, response: ProviderResponse
) -> UsageMetering:
    req = dispatch_request.request
    input_bytes = (
        len(req.system_seed.encode("utf-8"))
        + len(req.user_input.encode("utf-8"))
        + sum(
            len(msg.role.encode("utf-8")) + len(msg.content.encode("utf-8"))
            for msg in req.history
        )
        + sum(
            len(fact.category.encode("utf-8")) + len(fact.value.encode("utf-8"))
            for fact in req.facts
        )
    )
    uncached_input_tokens = ((input_bytes + 3) // 4) + 32
    output_bytes = len(response.text.encode("utf-8"))
    max_out = max(0, dispatch_request.options.max_output_tokens)
    raw_output_tokens = max(1, (output_bytes + 3) // 4)
    output_tokens = min(max_out, raw_output_tokens) if max_out > 0 else 0
    return UsageMetering(
        uncached_input_tokens=uncached_input_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=output_tokens,
        reasoning_tokens=0,
        model_id=dispatch_request.model_id,
        tier=dispatch_request.tier,
    )


class CountingFakeTransport:
    """Independent in-process fake transport recording every ``send()`` call."""

    def __init__(
        self,
        *,
        provider: ModelProvider | None = None,
        usage_override: UsageMetering
        | Callable[[ProviderDispatchRequest, ProviderResponse], UsageMetering]
        | None = None,
        receipt_override: Callable[
            [ProviderDispatchRequest, ProviderResponse], ProviderReceipt
        ]
        | None = None,
        error_to_raise: Exception | None = None,
    ) -> None:
        self.provider: ModelProvider = (
            provider if provider is not None else FakeProvider()
        )
        self._usage_override = usage_override
        self._receipt_override = receipt_override
        self._error_to_raise = error_to_raise
        self.calls: list[RecordedDispatch] = []
        self.requests: list[ProviderDispatchRequest] = []

    @property
    def call_count(self) -> int:
        """Total number of ``send()`` invocations observed."""
        return len(self.calls)

    def send(self, dispatch_request: ProviderDispatchRequest) -> ProviderReceipt:
        """Record the dispatch attempt and return a deterministic receipt."""
        self.calls.append(
            RecordedDispatch(
                attempt_id=dispatch_request.attempt_id,
                idempotency_key=dispatch_request.idempotency_key,
                model_id=dispatch_request.model_id,
                tier=dispatch_request.tier,
                timestamp=_utc_now_iso(),
            )
        )
        self.requests.append(dispatch_request)
        if self._error_to_raise is not None:
            raise self._error_to_raise

        response = self.provider.complete(dispatch_request.request)
        if self._receipt_override is not None:
            return self._receipt_override(dispatch_request, response)

        if callable(self._usage_override):
            usage = self._usage_override(dispatch_request, response)
        elif self._usage_override is not None:
            usage = self._usage_override
        else:
            usage = _default_usage(dispatch_request, response)

        return ProviderReceipt(
            receipt_id=f"accept-rcpt-{uuid.uuid4().hex}",
            attempt_id=dispatch_request.attempt_id,
            response=response,
            usage=usage,
            complete=True,
            finish_reason="completed",
        )


def _write_json_atomic(path: Path, payload: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def read_file_transport_calls(counter_path: Path) -> list[RecordedDispatch]:
    """Read cross-process transport call records under the counter file lock."""
    lock_path = counter_path.with_suffix(".lock")
    with acquire_exclusive_file_lock(lock_path, busy_timeout_ms=10_000):
        if not counter_path.exists():
            return []
        raw = json.loads(counter_path.read_text(encoding="utf-8"))
        return [
            RecordedDispatch(
                attempt_id=str(item["attempt_id"]),
                idempotency_key=str(item["idempotency_key"]),
                model_id=str(item["model_id"]),
                tier=str(item["tier"]),
                timestamp=str(item["timestamp"]),
            )
            for item in raw
        ]


class FileCountingFakeTransport:
    """File-locked atomic cross-process fake transport for subprocess tests."""

    def __init__(
        self,
        counter_path: Path,
        *,
        provider: ModelProvider | None = None,
        hold_seconds: float = 0.0,
        crash_after_record: bool = False,
        on_send: Callable[[str], None] | None = None,
    ) -> None:
        self._counter_path = counter_path
        self._lock_path = counter_path.with_suffix(".lock")
        self.provider: ModelProvider = (
            provider if provider is not None else FakeProvider()
        )
        self._hold_seconds = hold_seconds
        self._crash_after_record = crash_after_record
        self._on_send = on_send

    def send(self, dispatch_request: ProviderDispatchRequest) -> ProviderReceipt:
        """Atomically append dispatch call metadata to disk, optionally hold/crash."""
        record = RecordedDispatch(
            attempt_id=dispatch_request.attempt_id,
            idempotency_key=dispatch_request.idempotency_key,
            model_id=dispatch_request.model_id,
            tier=dispatch_request.tier,
            timestamp=_utc_now_iso(),
        )
        with acquire_exclusive_file_lock(self._lock_path, busy_timeout_ms=10_000):
            existing: list[dict[str, str]]
            if self._counter_path.exists():
                existing = json.loads(self._counter_path.read_text(encoding="utf-8"))
            else:
                existing = []
            existing.append(asdict(record))
            _write_json_atomic(self._counter_path, existing)

        if self._crash_after_record:
            os._exit(1)

        if self._on_send is not None:
            self._on_send(dispatch_request.attempt_id)

        if self._hold_seconds > 0:
            time.sleep(self._hold_seconds)

        response = self.provider.complete(dispatch_request.request)
        usage = _default_usage(dispatch_request, response)
        return ProviderReceipt(
            receipt_id=f"file-rcpt-{uuid.uuid4().hex}",
            attempt_id=dispatch_request.attempt_id,
            response=response,
            usage=usage,
            complete=True,
            finish_reason="completed",
        )


def _installed_bunny() -> Path:
    bin_dir = Path(sys.executable).parent
    bunny = bin_dir / ("bunny.exe" if os.name == "nt" else "bunny")
    assert bunny.is_file(), f"Installed CLI not found at {bunny}"
    return bunny


def _subprocess_env(tmp_path: Path, *, include_tests: bool = False) -> dict[str, str]:
    guard_dir = tmp_path / "_net_guard"
    guard_dir.mkdir(exist_ok=True)
    (guard_dir / "sitecustomize.py").write_text(NETWORK_GUARD, encoding="utf-8")

    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}
    }
    profile_root = tmp_path / "_isolated_home"
    profile_root.mkdir(exist_ok=True)
    pythonpath = str(guard_dir)
    if include_tests:
        pythonpath = f"{guard_dir}{os.pathsep}{Path(__file__).resolve().parent}"
    env.update(
        PYTHONPATH=pythonpath,
        PYTHONNOUSERSITE="1",
        PYTHONUTF8="1",
        HOME=str(profile_root),
        USERPROFILE=str(profile_root),
        APPDATA=str(profile_root / "appdata"),
        LOCALAPPDATA=str(profile_root / "localappdata"),
        XDG_CONFIG_HOME=str(profile_root / "xdg-config"),
        XDG_DATA_HOME=str(profile_root / "xdg-data"),
    )
    return env


def _run_cli(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_installed_bunny()), *args],
        cwd=cwd,
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )


def _sample_request(
    identity_id: str = SYNTHETIC_IDENTITY_ID,
    *,
    user_input: str = "Hello Bunny",
    facts: tuple[ContextFact, ...] = (),
) -> ProviderRequest:
    return ProviderRequest(
        identity_id=identity_id,
        seed_version="seed-1",
        style_version="style-1",
        system_seed=SeedPersonality().render_system_seed(),
        history=(),
        user_input=user_input,
        facts=facts,
    )


def _open_ledger(data_dir: Path) -> tuple[LocalStorage, BudgetLedger, str]:
    storage = LocalStorage(data_dir)
    init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    ledger = BudgetLedger(storage, init.identity.identity_id)
    ledger.check_and_reconcile_watermark(FIXED_NOW)
    return storage, ledger, init.identity.identity_id


def _reservation_for(
    user_input: str,
    *,
    profile: PriceProfile | None = None,
    max_output_tokens: int = 1_024,
) -> int:
    prof = DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID] if profile is None else profile
    req = _sample_request(user_input=user_input)
    bound = estimate_conservative_input_tokens(req, prof)
    return calculate_reservation_micro_usd(bound, max_output_tokens, prof)


# ---------------------------------------------------------------------------
# Decisive Test 1 — Two processes racing for the last available allowance
# ---------------------------------------------------------------------------


def test_decisive_1_two_processes_race_for_last_available_allowance(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "race-profile"
    counter_path = tmp_path / "transport-calls.json"
    message = "Race for the final reservation"
    reserved = _reservation_for(message)

    storage, ledger, _ = _open_ledger(data_dir)
    try:
        ledger.set_allowances(
            per_request_micro_usd=reserved,
            cumulative_micro_usd=reserved,
            now_utc=FIXED_NOW,
        )
    finally:
        storage.close()

    ready_a = tmp_path / "ready_a"
    ready_b = tmp_path / "ready_b"
    go_file = tmp_path / "go"

    worker_script = """
import json
import sys
import time
from pathlib import Path
from bunny_ai.app import AwakeningSession
from bunny_ai.config import load_config
from test_gateway_acceptance import FileCountingFakeTransport

data_dir = Path(sys.argv[1])
counter_path = Path(sys.argv[2])
ready_file = Path(sys.argv[3])
go_file = Path(sys.argv[4])
message = sys.argv[5]

cfg = load_config(cli_data_dir=data_dir)
transport = FileCountingFakeTransport(counter_path, hold_seconds=0.4)
with AwakeningSession(cfg, transport=transport) as session:
    ready_file.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10.0
    while not go_file.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for race barrier")
        time.sleep(0.005)
    outcome = session.handle_turn(message)
    print(
        json.dumps(
            {
                "status": outcome.status,
                "refusal_code": outcome.refusal_code,
                "attempt_id": outcome.attempt_id,
                "reserved_micro_usd": outcome.reserved_micro_usd,
                "settled_micro_usd": outcome.settled_micro_usd,
                "held_micro_usd": outcome.held_micro_usd,
            }
        )
    )
"""

    env = _subprocess_env(tmp_path, include_tests=True)
    proc_a = subprocess.Popen(
        [
            sys.executable,
            "-c",
            worker_script,
            str(data_dir),
            str(counter_path),
            str(ready_a),
            str(go_file),
            message,
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    proc_b = subprocess.Popen(
        [
            sys.executable,
            "-c",
            worker_script,
            str(data_dir),
            str(counter_path),
            str(ready_b),
            str(go_file),
            message,
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    try:
        barrier_deadline = time.monotonic() + 10.0
        while not (ready_a.exists() and ready_b.exists()):
            assert proc_a.poll() is None, "Worker A exited prematurely"
            assert proc_b.poll() is None, "Worker B exited prematurely"
            if time.monotonic() >= barrier_deadline:
                pytest.fail("Timed out waiting for worker processes to reach barrier")
            time.sleep(0.01)

        go_file.write_text("go", encoding="utf-8")
        out_a, err_a = proc_a.communicate(timeout=20)
        out_b, err_b = proc_b.communicate(timeout=20)
    finally:
        for proc in (proc_a, proc_b):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    assert proc_a.returncode == 0, err_a
    assert proc_b.returncode == 0, err_b
    assert err_a == ""
    assert err_b == ""

    res_a = json.loads(out_a.strip())
    res_b = json.loads(out_b.strip())
    outcomes = [res_a, res_b]
    settled_list = [o for o in outcomes if o["status"] == "settled"]
    refused_list = [o for o in outcomes if o["status"] == "refused"]

    assert len(settled_list) == 1, outcomes
    assert len(refused_list) == 1, outcomes
    assert refused_list[0]["refusal_code"] == "cumulative_budget_exceeded"
    assert settled_list[0]["settled_micro_usd"] > 0
    assert settled_list[0]["held_micro_usd"] == 0

    calls = read_file_transport_calls(counter_path)
    assert len(calls) == 1
    assert calls[0].attempt_id == settled_list[0]["attempt_id"]
    assert calls[0].model_id == "sim-neon-1"
    assert calls[0].tier == "standard"

    storage, ledger, _ = _open_ledger(data_dir)
    try:
        summary = ledger.summary()
        assert summary.cumulative_micro_usd == reserved
        assert summary.settled_micro_usd == settled_list[0]["settled_micro_usd"]
        assert summary.settled_micro_usd <= reserved
        assert summary.held_micro_usd == 0
        assert summary.settled_count == 1
        assert summary.refused_count == 1
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# Decisive Test 2 — Consent revoked by another process before dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["memory_off", "memory_delete"])
def test_decisive_2_consent_revoked_by_other_process_before_dispatch(
    tmp_path: Path, variant: str
) -> None:
    env = _subprocess_env(tmp_path)
    data_dir = tmp_path / f"consent-{variant}"

    seed = _run_cli(
        ["chat", "--data-dir", str(data_dir)],
        env=env,
        cwd=tmp_path,
        stdin_text="/skip\n/remember name Zorblax\ny\n/exit\n",
    )
    assert seed.returncode == 0, seed.stderr

    storage, ledger, identity_id = _open_ledger(data_dir)
    try:
        store = MemoryStore(storage, identity_id)
        facts = store.list_facts()
        assert store.settings().enabled is True
        assert len(facts) == 1
        fact = facts[0]
        assert fact.value == "Zorblax"

        def _revoke_in_other_cli_process() -> None:
            cmd_line = (
                "/memory off\n/exit\n"
                if variant == "memory_off"
                else f"/memory delete {fact.fact_id[:8]}\n/exit\n"
            )
            revoked = _run_cli(
                ["chat", "--data-dir", str(data_dir)],
                env=env,
                cwd=tmp_path,
                stdin_text=cmd_line,
            )
            assert revoked.returncode == 0, revoked.stderr

        transport = CountingFakeTransport()
        gateway = ModelGateway(
            storage,
            ledger,
            transport,
            clock=lambda: FIXED_NOW,
            before_authorize=_revoke_in_other_cli_process,
        )
        consent = compute_consent_snapshot(storage, identity_id)
        request = _sample_request(
            identity_id,
            user_input="What is my preferred name?",
            facts=(
                ContextFact(
                    fact_id=fact.fact_id,
                    category="preferred_name",
                    value=fact.value,
                ),
            ),
        )
        prepared = gateway.prepare(
            profile_id=DEFAULT_PROFILE_ID,
            request=request,
            consent=consent,
        )
        outcome = gateway.dispatch(prepared)

        assert outcome.status == "refused"
        assert outcome.refusal_code == "consent_changed"
        assert transport.call_count == 0

        summary = ledger.summary()
        assert summary.settled_micro_usd == 0
        assert summary.held_micro_usd == 0
        assert summary.refused_count == 1
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# Decisive Test 3 — Crash after dispatch intent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["before_send", "after_send", "after_transport"])
def test_decisive_3_crash_after_dispatch_intent(tmp_path: Path, variant: str) -> None:
    data_dir = tmp_path / f"crash-{variant}"
    counter_path = tmp_path / f"counter-{variant}.json"
    message = "Crash test prompt"
    reserved = _reservation_for(message)

    storage, ledger, _ = _open_ledger(data_dir)
    try:
        ledger.set_allowances(
            per_request_micro_usd=reserved,
            cumulative_micro_usd=reserved,
            now_utc=FIXED_NOW,
        )
    finally:
        storage.close()

    crash_script = """
import os
import sys
from pathlib import Path
from bunny_ai.budget import DEFAULT_PROFILE_ID
from bunny_ai.gateway import ModelGateway
from bunny_ai.ledger import BudgetLedger
from bunny_ai.neon import SeedPersonality
from bunny_ai.providers import ProviderRequest, RequestOptions
from bunny_ai.storage import LocalStorage
from test_gateway_acceptance import FIXED_NOW, FileCountingFakeTransport

data_dir = Path(sys.argv[1])
counter_path = Path(sys.argv[2])
variant = sys.argv[3]
message = sys.argv[4]

storage = LocalStorage(data_dir)
init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
ledger = BudgetLedger(storage, init.identity.identity_id)
ledger.check_and_reconcile_watermark(FIXED_NOW)

transport = FileCountingFakeTransport(
    counter_path, crash_after_record=(variant == "after_send")
)
after_reserve = (lambda _aid: os._exit(1)) if variant == "before_send" else None
after_transport = (
    (lambda _aid, _rcpt: os._exit(1)) if variant == "after_transport" else None
)
gateway = ModelGateway(
    storage,
    ledger,
    transport,
    clock=lambda: FIXED_NOW,
    after_reserve=after_reserve,
    after_transport=after_transport,
)
req = ProviderRequest(
    identity_id=init.identity.identity_id,
    seed_version="seed-1",
    style_version="style-1",
    system_seed=SeedPersonality().render_system_seed(),
    history=(),
    user_input=message,
    facts=(),
)
prepared = gateway.prepare(
    profile_id=DEFAULT_PROFILE_ID,
    request=req,
    options=RequestOptions(max_output_tokens=1_024),
)
gateway.dispatch(prepared)
"""

    env = _subprocess_env(tmp_path, include_tests=True)
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            crash_script,
            str(data_dir),
            str(counter_path),
            variant,
            message,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert crashed.returncode != 0

    expected_calls = 0 if variant == "before_send" else 1
    assert len(read_file_transport_calls(counter_path)) == expected_calls

    # Restart 1: orphaned reserved intent becomes uncertain_hold.
    cfg = load_config(cli_data_dir=data_dir)
    with AwakeningSession(
        cfg,
        transport=FileCountingFakeTransport(counter_path),
        clock=lambda: FIXED_NOW + timedelta(hours=1),
    ) as session1:
        s1 = session1.ledger.summary()
        assert s1.uncertain_hold_count == 1
        assert s1.held_micro_usd == reserved
        assert s1.settled_micro_usd == 0
        assert s1.available_micro_usd == 0
        attempts1 = session1.ledger.list_attempts(limit=5)
        assert len(attempts1) == 1
        assert attempts1[0].state == "uncertain_hold"
        held_attempt_id = attempts1[0].attempt_id

    assert len(read_file_transport_calls(counter_path)) == expected_calls

    # Restart 2 (30 days later): hold is STILL retained, never expires, never replayed.
    with AwakeningSession(
        cfg,
        transport=FileCountingFakeTransport(counter_path),
        clock=lambda: FIXED_NOW + timedelta(days=30),
    ) as session2:
        s2 = session2.ledger.summary()
        assert s2.uncertain_hold_count == 1
        assert s2.held_micro_usd == reserved
        assert s2.settled_micro_usd == 0
        assert s2.available_micro_usd == 0

        refused = session2.handle_turn("New turn while uncertain hold is open")
        assert refused.status == "refused"
        assert refused.refusal_code == "cumulative_budget_exceeded"
        assert len(read_file_transport_calls(counter_path)) == expected_calls

    # Manual reconciliation via CLI clears the hold cleanly.
    if variant == "before_send":
        rec = _run_cli(
            [
                "budget",
                "reconcile",
                held_attempt_id[:8],
                "--release",
                "--data-dir",
                str(data_dir),
            ],
            env=env,
            cwd=tmp_path,
        )
        assert rec.returncode == 0, rec.stderr
        assert "released" in rec.stdout

        storage, ledger, _ = _open_ledger(data_dir)
        try:
            final = ledger.summary()
            assert final.held_micro_usd == 0
            assert final.uncertain_hold_count == 0
            assert final.released_count == 1
            assert final.settled_micro_usd == 0
            assert final.available_micro_usd == reserved
        finally:
            storage.close()
    else:
        reconciled_spend = reserved // 2
        assert reconciled_spend > 0
        rec = _run_cli(
            [
                "budget",
                "reconcile",
                held_attempt_id[:8],
                "--settle",
                str(reconciled_spend),
                "--data-dir",
                str(data_dir),
            ],
            env=env,
            cwd=tmp_path,
        )
        assert rec.returncode == 0, rec.stderr
        assert "settled" in rec.stdout

        storage, ledger, _ = _open_ledger(data_dir)
        try:
            final = ledger.summary()
            assert final.held_micro_usd == 0
            assert final.uncertain_hold_count == 0
            assert final.settled_count == 1
            assert final.settled_micro_usd == reconciled_spend
            assert final.available_micro_usd == reserved - reconciled_spend
        finally:
            storage.close()

    assert len(read_file_transport_calls(counter_path)) == expected_calls


# ---------------------------------------------------------------------------
# Decisive Test 4 — Duplicate receipt
# ---------------------------------------------------------------------------


def test_decisive_4_duplicate_receipt_idempotent_with_frozen_pricing(
    tmp_path: Path,
) -> None:
    base_profile = DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID]
    profiles: dict[str, PriceProfile | None] = {DEFAULT_PROFILE_ID: base_profile}
    transport = CountingFakeTransport()

    storage, ledger, identity_id = _open_ledger(tmp_path / "dup-receipt")
    try:
        gateway = ModelGateway(
            storage,
            ledger,
            transport,
            price_profiles=profiles,
            clock=lambda: FIXED_NOW,
        )
        prepared = gateway.prepare(
            profile_id=DEFAULT_PROFILE_ID,
            request=_sample_request(identity_id, user_input="Settle me once"),
        )
        out = gateway.dispatch(prepared)
        assert out.status == "settled"
        assert out.receipt is not None
        assert transport.call_count == 1
        original_settled = out.settled_micro_usd
        assert original_settled > 0

        # Mutate the active price profile to 10x higher rates before re-delivery.
        profiles[DEFAULT_PROFILE_ID] = replace(
            base_profile,
            price_version="sim-2026-11-v2-higher",
            input_micro_usd_per_1m=base_profile.input_micro_usd_per_1m * 10,
            cache_read_micro_usd_per_1m=base_profile.cache_read_micro_usd_per_1m * 10,
            cache_write_micro_usd_per_1m=base_profile.cache_write_micro_usd_per_1m * 10,
            output_micro_usd_per_1m=base_profile.output_micro_usd_per_1m * 10,
        )

        # 1) Exact same ProviderReceipt re-delivered.
        again = gateway.settle_receipt(out.attempt_id, out.receipt)
        assert again.state == "settled"
        assert again.settled_micro_usd == original_settled
        assert again.held_micro_usd == 0

        # 2) Duplicate receipt_id with altered/inflated usage delivered to same attempt.
        inflated_dup = replace(
            out.receipt,
            usage=UsageMetering(
                uncached_input_tokens=9_999,
                cache_read_tokens=0,
                cache_write_tokens=0,
                output_tokens=999,
                model_id=base_profile.model_id,
                tier=base_profile.tier,
            ),
        )
        again_inflated = gateway.settle_receipt(out.attempt_id, inflated_dup)
        assert again_inflated.settled_micro_usd == original_settled
        assert again_inflated.held_micro_usd == 0

        # 3) Duplicate receipt_id delivered to a different reserved attempt is rejected.
        prep2 = gateway.prepare(
            profile_id=DEFAULT_PROFILE_ID,
            request=_sample_request(identity_id, user_input="Second attempt"),
        )
        ledger.reserve_attempt(
            attempt_id=prep2.attempt_id,
            idempotency_key=prep2.idempotency_key,
            profile=base_profile,
            request_digest=prep2.request_digest,
            expected_consent_digest=prep2.consent_digest,
            reserved_micro_usd=prep2.reserved_micro_usd,
            now_utc=FIXED_NOW,
        )
        with pytest.raises(
            GatewayError, match="already belongs to a different attempt"
        ):
            gateway.settle_receipt(prep2.attempt_id, out.receipt)
        ledger.release_not_dispatched(
            prep2.attempt_id,
            reason="cleanup_after_dup_receipt_check",
            now_utc=FIXED_NOW,
        )

        summary = ledger.summary()
        assert summary.settled_micro_usd == original_settled
        assert summary.held_micro_usd == 0
        assert summary.settled_count == 1
        assert transport.call_count == 1
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# Decisive Test 5 — Older backup restored after spending
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["cli_recover", "disk_overwrite"])
def test_decisive_5_older_backup_restored_after_spending(
    tmp_path: Path, variant: str
) -> None:
    env = _subprocess_env(tmp_path)
    data_dir = tmp_path / f"rollback-{variant}"
    base_profile = DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID]
    fixed_usage = UsageMetering(
        uncached_input_tokens=100,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=50,
        model_id=base_profile.model_id,
        tier=base_profile.tier,
    )
    per_call_cost = ceil_div(
        100 * base_profile.input_micro_usd_per_1m
        + 50 * base_profile.output_micro_usd_per_1m,
        1_000_000,
    )
    assert per_call_cost == 35
    reserved = _reservation_for("Spend turn")
    cumulative_cap = reserved + per_call_cost

    # Seed profile with zero spend and take pre-spend backup.
    storage, ledger, identity_id = _open_ledger(data_dir)
    try:
        ledger.set_allowances(
            per_request_micro_usd=reserved,
            cumulative_micro_usd=cumulative_cap,
            now_utc=FIXED_NOW,
        )
        assert ledger.summary().settled_micro_usd == 0
        backup_path = storage.create_backup("pre-spend")
    finally:
        storage.close()

    # Execute 2 accepted dispatches so settled > 0 and remaining < 1 full reservation.
    spend_transport = CountingFakeTransport(usage_override=fixed_usage)
    storage, ledger, identity_id = _open_ledger(data_dir)
    try:
        gateway = ModelGateway(
            storage, ledger, spend_transport, clock=lambda: FIXED_NOW
        )
        for idx in (1, 2):
            prep = gateway.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Spend turn"),
                idempotency_key=f"spend-key-{idx}",
            )
            out = gateway.dispatch(prep)
            assert out.status == "settled"
            assert out.settled_micro_usd == per_call_cost
        assert spend_transport.call_count == 2
        total_spent = ledger.summary().settled_micro_usd
        assert total_spent == per_call_cost * 2
        assert ledger.summary().available_micro_usd == cumulative_cap - total_spent
    finally:
        storage.close()

    # Restore older backup via CLI or direct disk overwrite.
    if variant == "cli_recover":
        rec = _run_cli(
            [
                "recover",
                "--data-dir",
                str(data_dir),
                "--backup",
                backup_path.name,
                "--yes",
            ],
            env=env,
            cwd=tmp_path,
        )
        assert rec.returncode == 0, rec.stderr
        assert "BLOCKED" in rec.stdout
    else:
        shutil.copy2(backup_path, data_dir / DB_FILENAME)

    # Read-only status and LocalStorage.inspect() both see prior spend & block.
    status_res = _run_cli(
        ["status", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert status_res.returncode == 0, status_res.stderr
    assert f"Budget settled: {format_micro_usd(total_spent)}" in status_res.stdout
    assert "Dispatch status: BLOCKED" in status_res.stdout

    snap = LocalStorage(data_dir).inspect()
    assert snap.settled_micro_usd == total_spent
    assert snap.available_micro_usd == cumulative_cap - total_spent
    assert snap.dispatch_blocked is True

    # Replay of used idempotency key and any new dispatch are both refused with 0 calls.
    post_transport = CountingFakeTransport(usage_override=fixed_usage)
    storage, ledger, identity_id = _open_ledger(data_dir)
    try:
        gateway = ModelGateway(storage, ledger, post_transport, clock=lambda: FIXED_NOW)
        replay_blocked = gateway.dispatch(
            gateway.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Spend turn"),
                idempotency_key="spend-key-1",
            )
        )
        assert replay_blocked.status == "refused"
        assert replay_blocked.refusal_code in (
            "stale_backup_restored_reconcile_required",
            "stale_backup_detected_reconcile_required",
        )

        new_blocked = gateway.dispatch(
            gateway.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Spend turn"),
                idempotency_key="spend-key-3",
            )
        )
        assert new_blocked.status == "refused"
        assert new_blocked.refusal_code in (
            "stale_backup_restored_reconcile_required",
            "stale_backup_detected_reconcile_required",
        )
        assert post_transport.call_count == 0
    finally:
        storage.close()

    # Clear review via CLI: prior spend stays permanently deducted, replay still refused.
    clear_res = _run_cli(
        ["budget", "clear-review", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert clear_res.returncode == 0, clear_res.stderr
    assert "Dispatch status: ok" in clear_res.stdout
    assert f"Settled: {format_micro_usd(total_spent)}" in clear_res.stdout

    storage, ledger, identity_id = _open_ledger(data_dir)
    try:
        summary = ledger.summary()
        assert summary.dispatch_blocked is False
        assert summary.settled_micro_usd == total_spent
        assert summary.available_micro_usd == cumulative_cap - total_spent
        assert summary.available_micro_usd < reserved

        gateway = ModelGateway(storage, ledger, post_transport, clock=lambda: FIXED_NOW)
        replay_after = gateway.dispatch(
            gateway.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Spend turn"),
                idempotency_key="spend-key-1",
            )
        )
        assert replay_after.status == "refused"
        assert replay_after.refusal_code == "duplicate_idempotency_key"

        new_after = gateway.dispatch(
            gateway.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Spend turn"),
                idempotency_key="spend-key-4",
            )
        )
        assert new_after.status == "refused"
        assert new_after.refusal_code == "cumulative_budget_exceeded"
        assert post_transport.call_count == 0
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# Additional required acceptance coverage
# ---------------------------------------------------------------------------


def test_pricing_missing_incomplete_expired_and_not_yet_valid_refused(
    tmp_path: Path,
) -> None:
    base = DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID]
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(
        json.dumps(
            {
                "profiles": {
                    "sim-malformed": {"model_id": "only-one-field"},
                    "sim-zero-rates": {
                        "provider_id": "simulated",
                        "model_id": "sim-zero",
                        "tier": "standard",
                        "endpoint": "simulated://local/v1/responses",
                        "price_version": "v1",
                        "valid_from_utc": "2026-01-01T00:00:00Z",
                        "valid_until_utc": "2027-12-31T23:59:59Z",
                        "input_micro_usd_per_1m": 0,
                        "cache_read_micro_usd_per_1m": 0,
                        "cache_write_micro_usd_per_1m": 0,
                        "output_micro_usd_per_1m": 0,
                        "max_input_tokens": 4096,
                        "max_output_tokens": 1024,
                        "max_input_chars": 16384,
                    },
                    "sim-expired": {
                        "provider_id": "simulated",
                        "model_id": "sim-old",
                        "tier": "standard",
                        "endpoint": "simulated://local/v1/responses",
                        "price_version": "v1",
                        "valid_from_utc": "2025-01-01T00:00:00Z",
                        "valid_until_utc": "2026-11-27T11:59:59Z",
                        "input_micro_usd_per_1m": 100_000,
                        "cache_read_micro_usd_per_1m": 10_000,
                        "cache_write_micro_usd_per_1m": 125_000,
                        "output_micro_usd_per_1m": 500_000,
                        "max_input_tokens": 4096,
                        "max_output_tokens": 1024,
                        "max_input_chars": 16384,
                    },
                    "sim-future": {
                        "provider_id": "simulated",
                        "model_id": "sim-next",
                        "tier": "standard",
                        "endpoint": "simulated://local/v1/responses",
                        "price_version": "v1",
                        "valid_from_utc": "2026-11-27T12:00:01Z",
                        "valid_until_utc": "2027-12-31T23:59:59Z",
                        "input_micro_usd_per_1m": 100_000,
                        "cache_read_micro_usd_per_1m": 10_000,
                        "cache_write_micro_usd_per_1m": 125_000,
                        "output_micro_usd_per_1m": 500_000,
                        "max_input_tokens": 4096,
                        "max_output_tokens": 1024,
                        "max_input_chars": 16384,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    loaded = load_pricing_file(pricing_file)
    loaded["sim-incomplete-lc"] = replace(
        base,
        profile_id="sim-incomplete-lc",
        long_context_threshold_tokens=1_000,
        long_context_input_micro_usd_per_1m=None,
        long_context_output_micro_usd_per_1m=None,
    )
    loaded["sim-short-window"] = replace(
        base,
        profile_id="sim-short-window",
        valid_from_utc="2026-11-27T11:00:00Z",
        valid_until_utc="2026-11-27T12:05:00Z",
    )

    clock_now = FIXED_NOW
    transport = CountingFakeTransport()
    storage, ledger, identity_id = _open_ledger(tmp_path / "pricing-profile")
    try:
        gateway = ModelGateway(
            storage,
            ledger,
            transport,
            price_profiles=loaded,
            clock=lambda: clock_now,
        )
        cases = [
            ("sim-nonexistent", "pricing_missing"),
            ("sim-malformed", "pricing_incomplete"),
            ("sim-zero-rates", "pricing_incomplete"),
            ("sim-incomplete-lc", "pricing_incomplete"),
            ("sim-expired", "pricing_expired"),
            ("sim-future", "pricing_expired"),
        ]
        for profile_id, expected_code in cases:
            out = gateway.dispatch(
                gateway.prepare(
                    profile_id=profile_id,
                    request=_sample_request(identity_id),
                )
            )
            assert out.status == "refused"
            assert out.refusal_code == expected_code

        # Valid at prepare(), expired before dispatch() -> refused on dispatch.
        prepared_valid = gateway.prepare(
            profile_id="sim-short-window",
            request=_sample_request(identity_id),
        )
        assert prepared_valid.refusal_code is None
        clock_now = FIXED_NOW + timedelta(minutes=10)
        expired_mid_flight = gateway.dispatch(prepared_valid)
        assert expired_mid_flight.status == "refused"
        assert expired_mid_flight.refusal_code == "pricing_expired"

        assert transport.call_count == 0
        summary = ledger.summary()
        assert summary.settled_micro_usd == 0
        assert summary.held_micro_usd == 0
        assert summary.refused_count == len(cases) + 1
    finally:
        storage.close()


def test_allowance_missing_and_per_request_cap_exceeded_refused(
    tmp_path: Path,
) -> None:
    transport = CountingFakeTransport()
    data_dir = tmp_path / "allowance-profile"
    storage, ledger, identity_id = _open_ledger(data_dir)
    try:
        gateway = ModelGateway(storage, ledger, transport, clock=lambda: FIXED_NOW)
        req = _sample_request(identity_id, user_input="Check allowance")
        reserved = _reservation_for("Check allowance")

        # 1) NULL per_request_micro_usd -> allowance_missing.
        with storage.transaction("clear per_request cap") as conn:
            conn.execute(
                "UPDATE budget_policy SET per_request_micro_usd = NULL WHERE singleton = 1"
            )
        out1 = gateway.dispatch(
            gateway.prepare(profile_id=DEFAULT_PROFILE_ID, request=req)
        )
        assert out1.status == "refused"
        assert out1.refusal_code == "allowance_missing"

        # 2) NULL cumulative_micro_usd -> allowance_missing.
        with storage.transaction("clear cumulative cap") as conn:
            conn.execute(
                "UPDATE budget_policy SET per_request_micro_usd = ?, cumulative_micro_usd = NULL WHERE singleton = 1",
                (reserved,),
            )
        out2 = gateway.dispatch(
            gateway.prepare(profile_id=DEFAULT_PROFILE_ID, request=req)
        )
        assert out2.status == "refused"
        assert out2.refusal_code == "allowance_missing"

        # 3) Per-request cap below reservation -> per_request_budget_exceeded.
        ledger.set_allowances(
            per_request_micro_usd=reserved - 1,
            cumulative_micro_usd=reserved * 10,
            now_utc=FIXED_NOW,
        )
        out3 = gateway.dispatch(
            gateway.prepare(profile_id=DEFAULT_PROFILE_ID, request=req)
        )
        assert out3.status == "refused"
        assert out3.refusal_code == "per_request_budget_exceeded"

        assert transport.call_count == 0
        summary = ledger.summary()
        assert summary.settled_micro_usd == 0
        assert summary.held_micro_usd == 0
        assert summary.refused_count == 3
    finally:
        storage.close()


def test_overrun_blocked_records_actual_charge_and_blocks_next_dispatch(
    tmp_path: Path,
) -> None:
    profile = DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID]
    overrun_usage = UsageMetering(
        uncached_input_tokens=1_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=2_000,
        model_id=profile.model_id,
        tier=profile.tier,
    )
    expected_actual = ceil_div(
        1_000 * profile.input_micro_usd_per_1m
        + 2_000 * profile.output_micro_usd_per_1m,
        1_000_000,
    )
    transport = CountingFakeTransport(usage_override=overrun_usage)

    storage, ledger, identity_id = _open_ledger(tmp_path / "overrun-profile")
    try:
        gateway = ModelGateway(storage, ledger, transport, clock=lambda: FIXED_NOW)
        prep = gateway.prepare(
            profile_id=DEFAULT_PROFILE_ID,
            request=_sample_request(identity_id, user_input="Trigger overrun"),
            options=RequestOptions(max_output_tokens=64),
        )
        assert expected_actual > prep.reserved_micro_usd

        out = gateway.dispatch(prep)
        assert out.status == "overrun_blocked"
        assert out.settled_micro_usd == expected_actual
        assert out.overrun_micro_usd == expected_actual - prep.reserved_micro_usd
        assert out.held_micro_usd == 0
        assert transport.call_count == 1

        summary = ledger.summary()
        assert summary.settled_micro_usd == expected_actual
        assert summary.held_micro_usd == 0
        assert summary.dispatch_blocked is True
        assert summary.block_reason == "budget_overrun"
        assert summary.overrun_blocked_count == 1

        # Next dispatch is refused with 0 additional transport calls.
        blocked = gateway.dispatch(
            gateway.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Blocked follow-up"),
            )
        )
        assert blocked.status == "refused"
        assert blocked.refusal_code == "budget_overrun"
        assert transport.call_count == 1
    finally:
        storage.close()


@pytest.mark.parametrize(
    ("bad_model", "bad_tier"),
    [("wrong-model-id", "standard"), ("sim-neon-1", "batch")],
)
def test_receipt_model_and_tier_mismatch_blocks_dispatch(
    tmp_path: Path, bad_model: str, bad_tier: str
) -> None:
    mismatch_usage = UsageMetering(
        uncached_input_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=20,
        model_id=bad_model,
        tier=bad_tier,
    )
    transport = CountingFakeTransport(usage_override=mismatch_usage)

    storage, ledger, identity_id = _open_ledger(
        tmp_path / f"mismatch-{bad_model}-{bad_tier}"
    )
    try:
        gateway = ModelGateway(storage, ledger, transport, clock=lambda: FIXED_NOW)
        prep = gateway.prepare(
            profile_id=DEFAULT_PROFILE_ID,
            request=_sample_request(identity_id, user_input="Mismatch test"),
        )
        out = gateway.dispatch(prep)
        assert out.status == "overrun_blocked"
        assert out.settled_micro_usd == 0
        assert out.held_micro_usd == prep.reserved_micro_usd
        assert transport.call_count == 1

        summary = ledger.summary()
        assert summary.dispatch_blocked is True
        assert summary.block_reason == "settlement_model_or_usage_mismatch"
        assert summary.overrun_blocked_count == 1
        assert summary.held_micro_usd == prep.reserved_micro_usd
        assert summary.settled_micro_usd == 0

        blocked = gateway.dispatch(
            gateway.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Blocked follow-up"),
            )
        )
        assert blocked.status == "refused"
        assert blocked.refusal_code == "settlement_model_or_usage_mismatch"
        assert transport.call_count == 1
    finally:
        storage.close()


def test_cache_aware_integer_ceiling_settlement_uses_frozen_snapshot(
    tmp_path: Path,
) -> None:
    frozen_rate_profile = PriceProfile(
        profile_id="sim-cache",
        provider_id="simulated",
        model_id="sim-neon-cache",
        tier="standard",
        endpoint="simulated://local/v1/responses",
        price_version="sim-cache-v1",
        valid_from_utc="2026-01-01T00:00:00Z",
        valid_until_utc="2027-12-31T23:59:59Z",
        input_micro_usd_per_1m=300_003,
        cache_read_micro_usd_per_1m=30_007,
        cache_write_micro_usd_per_1m=375_011,
        output_micro_usd_per_1m=1_500_013,
        max_input_tokens=4_096,
        max_output_tokens=1_024,
        max_input_chars=16_384,
    )
    profiles: dict[str, PriceProfile | None] = {"sim-cache": frozen_rate_profile}
    cache_usage = UsageMetering(
        uncached_input_tokens=17,
        cache_read_tokens=23,
        cache_write_tokens=11,
        output_tokens=31,
        reasoning_tokens=7,
        model_id="sim-neon-cache",
        tier="standard",
    )
    expected_numerator = 17 * 300_003 + 23 * 30_007 + 11 * 375_011 + 31 * 1_500_013
    expected_micro_usd = (expected_numerator + 1_000_000 - 1) // 1_000_000
    assert expected_numerator % 1_000_000 != 0

    transport = CountingFakeTransport(usage_override=cache_usage)
    data_dir = tmp_path / "cache-pricing"
    storage, ledger, identity_id = _open_ledger(data_dir)
    try:
        gateway = ModelGateway(
            storage,
            ledger,
            transport,
            price_profiles=profiles,
            clock=lambda: FIXED_NOW,
        )
        prep = gateway.prepare(
            profile_id="sim-cache",
            request=_sample_request(identity_id, user_input="Cache accounting"),
        )
        # Mutate active profile after prepare; reservation freezes original snapshot.
        profiles["sim-cache"] = replace(
            frozen_rate_profile,
            price_version="sim-cache-v2-mutated",
            input_micro_usd_per_1m=9_000_000,
            cache_read_micro_usd_per_1m=9_000_000,
            cache_write_micro_usd_per_1m=9_000_000,
            output_micro_usd_per_1m=9_000_000,
        )

        out = gateway.dispatch(prep)
        assert out.status == "settled"
        assert out.settled_micro_usd == expected_micro_usd
        assert out.held_micro_usd == 0
        assert transport.call_count == 1

        summary = ledger.summary()
        assert summary.settled_micro_usd == expected_micro_usd
        assert summary.held_micro_usd == 0

        attempt = ledger.list_attempts(limit=1)[0]
        assert attempt.uncached_input_tokens == 17
        assert attempt.cache_read_tokens == 23
        assert attempt.cache_write_tokens == 11
        assert attempt.output_tokens == 31
        assert attempt.reasoning_tokens == 7
        assert attempt.price_version == "sim-cache-v1"
    finally:
        storage.close()


def test_restart_memory_reset_history_clear_and_config_edit_preserve_ledger_state(
    tmp_path: Path,
) -> None:
    env = _subprocess_env(tmp_path)
    data_dir = tmp_path / "preserve-ledger"
    cfg_path = data_dir / "config.toml"
    data_dir.mkdir()
    cfg_path.write_text(
        "[history]\nenabled = true\n\n"
        "[budget]\nper_request_micro_usd = 50000\ncumulative_micro_usd = 500000\n",
        encoding="utf-8",
    )

    # 1) Create a confirmed memory fact and a persisted history turn via CLI.
    cli_seed = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--history"],
        env=env,
        cwd=tmp_path,
        stdin_text="/skip\n/remember name Zorblax\ny\nRemember this turn\n/exit\n",
    )
    assert cli_seed.returncode == 0, cli_seed.stderr
    assert "Turn spend:" in cli_seed.stdout

    # 2) Add an uncertain_hold and an overrun_blocked attempt via gateway.
    storage, ledger, identity_id = _open_ledger(data_dir)
    try:
        baseline_settled = ledger.summary().settled_micro_usd
        assert baseline_settled > 0

        uncertain_transport = CountingFakeTransport(
            receipt_override=lambda d, r: ProviderReceipt(
                receipt_id="rcpt-incomplete",
                attempt_id=d.attempt_id,
                response=r,
                usage=None,
                complete=False,
                finish_reason="interrupted",
            )
        )
        gw_uncertain = ModelGateway(
            storage, ledger, uncertain_transport, clock=lambda: FIXED_NOW
        )
        uncertain_out = gw_uncertain.dispatch(
            gw_uncertain.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Incomplete call"),
            )
        )
        assert uncertain_out.status == "uncertain_hold"
        assert uncertain_transport.call_count == 1
        expected_held = uncertain_out.held_micro_usd
        assert expected_held > 0

        profile = DEFAULT_PRICE_PROFILES[DEFAULT_PROFILE_ID]
        overrun_transport = CountingFakeTransport(
            usage_override=UsageMetering(
                uncached_input_tokens=1_000,
                cache_read_tokens=0,
                cache_write_tokens=0,
                output_tokens=2_000,
                model_id=profile.model_id,
                tier=profile.tier,
            )
        )
        gw_overrun = ModelGateway(
            storage, ledger, overrun_transport, clock=lambda: FIXED_NOW
        )
        overrun_out = gw_overrun.dispatch(
            gw_overrun.prepare(
                profile_id=DEFAULT_PROFILE_ID,
                request=_sample_request(identity_id, user_input="Overrun call"),
                options=RequestOptions(max_output_tokens=32),
            )
        )
        assert overrun_out.status == "overrun_blocked"
        assert overrun_transport.call_count == 1

        expected_settled = baseline_settled + overrun_out.settled_micro_usd
        before = ledger.summary()
        assert before.settled_micro_usd == expected_settled
        assert before.held_micro_usd == expected_held
        assert before.dispatch_blocked is True
    finally:
        storage.close()

    # 3) Run /memory reset and /history clear in CLI chat.
    cleanup_cli = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--history"],
        env=env,
        cwd=tmp_path,
        stdin_text="/memory reset\ny\n/history clear\ny\n/exit\n",
    )
    assert cleanup_cli.returncode == 0, cleanup_cli.stderr
    assert "Deleted 1 fact(s)." in cleanup_cli.stdout
    assert "Deleted 1 stored turn(s)." in cleanup_cli.stdout

    # 4) Edit config.toml to increase budget caps dramatically.
    cfg_path.write_text(
        "[history]\nenabled = true\n\n"
        "[budget]\nper_request_micro_usd = 9999999\ncumulative_micro_usd = 99999999\n",
        encoding="utf-8",
    )

    # 5) Restart session & CLI: settled, held, and dispatch_blocked remain intact.
    cfg: AppConfig = load_config(cli_data_dir=data_dir)
    verify_transport = CountingFakeTransport()
    with AwakeningSession(cfg, transport=verify_transport) as session:
        after = session.ledger.summary()
        assert after.settled_micro_usd == expected_settled
        assert after.held_micro_usd == expected_held
        assert after.dispatch_blocked is True
        assert after.block_reason == "budget_overrun"
        assert after.cumulative_micro_usd == 99_999_999
        assert (
            after.available_micro_usd == 99_999_999 - expected_settled - expected_held
        )

        refused = session.handle_turn("Should still be blocked after config edit")
        assert refused.status == "refused"
        assert refused.refusal_code == "budget_overrun"
        assert verify_transport.call_count == 0

    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone() == (0,)
    finally:
        conn.close()


def test_stage3_preservation_and_cli_budget_walkthrough(tmp_path: Path) -> None:
    env = _subprocess_env(tmp_path)
    data_dir = tmp_path / "cli-walkthrough"

    # 1) Accepted turn via `bunny chat -m ...` prints spend.
    accepted = _run_cli(
        ["chat", "--data-dir", str(data_dir), "--skip-intro", "-m", "Hello Bunny"],
        env=env,
        cwd=tmp_path,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "[Offline simulated | seed-1]" in accepted.stdout
    assert "Turn spend: $" in accepted.stdout

    # 2) `bunny budget` inspects caps, spend, holds, available, and recent attempts.
    budget_out = _run_cli(
        ["budget", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
    )
    assert budget_out.returncode == 0, budget_out.stderr
    assert "Per-request cap: $0.050000" in budget_out.stdout
    assert "Cumulative cap: $0.500000" in budget_out.stdout
    assert "Settled: $" in budget_out.stdout
    assert "Held: $0.000000" in budget_out.stdout
    assert "Available: $" in budget_out.stdout
    assert "Dispatch status: ok" in budget_out.stdout
    assert "Recent attempts:" in budget_out.stdout
    assert "settled" in budget_out.stdout

    # 3) Lower per-request cap via `bunny budget set` so next turn is refused.
    set_cap = _run_cli(
        ["budget", "set", "--data-dir", str(data_dir), "--per-request", "10"],
        env=env,
        cwd=tmp_path,
    )
    assert set_cap.returncode == 0, set_cap.stderr
    assert "Per-request cap: $0.000010" in set_cap.stdout

    refused = _run_cli(
        ["chat", "--data-dir", str(data_dir), "-m", "Should be refused"],
        env=env,
        cwd=tmp_path,
    )
    assert refused.returncode == 2
    assert "Dispatch refused (per_request_budget_exceeded):" in refused.stderr
    assert "settled $" in refused.stderr
    assert "held $0.000000" in refused.stderr
    assert "available $" in refused.stderr

    # 4) Stateless `bunny demo` via CLI subprocess creates zero files.
    demo_cwd = tmp_path / "demo-cwd"
    demo_cwd.mkdir()
    demo_env_dir = tmp_path / "demo-never-created"
    demo_env = dict(env)
    demo_env["BUNNY_DATA_DIR"] = str(demo_env_dir)
    demo_res = _run_cli(["demo", "Hello Bunny"], env=demo_env, cwd=demo_cwd)
    assert demo_res.returncode == 0, demo_res.stderr
    assert "[Offline demo, simulated] Received 11 characters." in demo_res.stdout
    assert list(demo_cwd.iterdir()) == []
    assert not demo_env_dir.exists()


def test_acceptance_in_process_session_uses_counting_transport(
    tmp_path: Path,
) -> None:
    """Verify AwakeningSession + Stage 3 memory recall routes through CountingFakeTransport."""
    data_dir = tmp_path / "session-recall"
    cfg = AppConfig(
        data_dir=data_dir,
        config_path=None,
        gateway=GatewaySettings(profile=DEFAULT_PROFILE_ID, output_limit=256),
    )
    transport = CountingFakeTransport()
    with AwakeningSession(cfg, transport=transport, clock=lambda: FIXED_NOW) as session:
        session.skip_onboarding()
        session.memory.save("preferred_name", "Quillon")
        out = session.handle_turn("What is my preferred name?")
        assert out.status == "settled"
        assert "Quillon" in out.reply
        assert out.settled_micro_usd > 0
        assert transport.call_count == 1
        assert transport.calls[0].attempt_id == out.attempt_id
        assert transport.calls[0].model_id == "sim-neon-1"
        assert transport.calls[0].tier == "standard"
        summary = session.ledger.summary()
        assert summary.settled_micro_usd == out.settled_micro_usd
        assert summary.held_micro_usd == 0


@pytest.mark.parametrize(
    "pause_point", ["after_reserve", "during_transport", "after_transport"]
)
def test_live_worker_paused_protects_reservation_from_second_process(
    tmp_path: Path, pause_point: str
) -> None:
    """A live worker paused across reserve/transport/pre-settle stays reserved across a 2nd process."""
    data_dir = tmp_path / f"live-{pause_point}"
    counter_path = tmp_path / f"live-counter-{pause_point}.json"
    paused_path = tmp_path / f"paused-{pause_point}.json"
    resume_path = tmp_path / f"resume-{pause_point}.flag"
    message = f"Live pause test ({pause_point})"
    reserved = _reservation_for(message)

    storage, ledger, _ = _open_ledger(data_dir)
    try:
        ledger.set_allowances(
            per_request_micro_usd=reserved * 4,
            cumulative_micro_usd=reserved * 4,
            now_utc=FIXED_NOW,
        )
    finally:
        storage.close()

    worker_script = """
import json
import sys
import time
from pathlib import Path
from bunny_ai.budget import DEFAULT_PROFILE_ID
from bunny_ai.gateway import ModelGateway
from bunny_ai.ledger import BudgetLedger
from bunny_ai.neon import SeedPersonality
from bunny_ai.providers import ProviderRequest, RequestOptions
from bunny_ai.storage import LocalStorage
from test_gateway_acceptance import FIXED_NOW, FileCountingFakeTransport

data_dir = Path(sys.argv[1])
counter_path = Path(sys.argv[2])
paused_path = Path(sys.argv[3])
resume_path = Path(sys.argv[4])
pause_point = sys.argv[5]
message = sys.argv[6]

def _wait_for_resume(attempt_id: str) -> None:
    tmp_file = paused_path.with_suffix(".tmp")
    tmp_file.write_text(json.dumps({"attempt_id": attempt_id}), encoding="utf-8")
    tmp_file.replace(paused_path)
    deadline = time.monotonic() + 25.0
    while not resume_path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for resume flag")
        time.sleep(0.02)

storage = LocalStorage(data_dir)
try:
    init = storage.initialize_or_load(seed_version="seed-1", style_version="style-1")
    ledger = BudgetLedger(storage, init.identity.identity_id)
    ledger.check_and_reconcile_watermark(FIXED_NOW)
    ledger.recover_stale_intents(FIXED_NOW)

    on_send = (
        (lambda aid: _wait_for_resume(aid)) if pause_point == "during_transport" else None
    )
    transport = FileCountingFakeTransport(counter_path, on_send=on_send)
    after_reserve = (
        (lambda aid: _wait_for_resume(aid)) if pause_point == "after_reserve" else None
    )
    after_transport = (
        (lambda aid, _rcpt: _wait_for_resume(aid))
        if pause_point == "after_transport"
        else None
    )
    gateway = ModelGateway(
        storage,
        ledger,
        transport,
        clock=lambda: FIXED_NOW,
        after_reserve=after_reserve,
        after_transport=after_transport,
    )
    req = ProviderRequest(
        identity_id=init.identity.identity_id,
        seed_version="seed-1",
        style_version="style-1",
        system_seed=SeedPersonality().render_system_seed(),
        history=(),
        user_input=message,
        facts=(),
    )
    prepared = gateway.prepare(
        profile_id=DEFAULT_PROFILE_ID,
        request=req,
        options=RequestOptions(max_output_tokens=1_024),
    )
    outcome = gateway.dispatch(prepared)
    print(
        json.dumps(
            {
                "status": outcome.status,
                "attempt_id": outcome.attempt_id,
                "reserved_micro_usd": outcome.reserved_micro_usd,
                "settled_micro_usd": outcome.settled_micro_usd,
                "held_micro_usd": outcome.held_micro_usd,
            }
        )
    )
finally:
    storage.close()
"""

    env = _subprocess_env(tmp_path, include_tests=True)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            worker_script,
            str(data_dir),
            str(counter_path),
            str(paused_path),
            str(resume_path),
            pause_point,
            message,
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 15.0
        while not paused_path.exists():
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(
                    f"Live worker exited early rc={proc.returncode}: {out}\n{err}"
                )
            if time.monotonic() > deadline:
                raise AssertionError("Timed out waiting for live worker to pause")
            time.sleep(0.02)

        attempt_id = str(
            json.loads(paused_path.read_text(encoding="utf-8"))["attempt_id"]
        )
        expected_mid_calls = 0 if pause_point == "after_reserve" else 1
        assert len(read_file_transport_calls(counter_path)) == expected_mid_calls

        # Second real Bunny chat process opens the profile while worker is paused.
        second_chat = _run_cli(
            ["chat", "--data-dir", str(data_dir)],
            env=env,
            cwd=tmp_path,
            stdin_text="/spend\n/exit\n",
        )
        assert second_chat.returncode == 0, second_chat.stderr
        assert f"[{attempt_id[:8]}] reserved" in second_chat.stdout
        assert "uncertain_hold" not in second_chat.stdout

        # Second `bunny budget` process also leaves the live reservation intact.
        budget_view = _run_cli(
            ["budget", "--data-dir", str(data_dir)], env=env, cwd=tmp_path
        )
        assert budget_view.returncode == 0, budget_view.stderr
        assert f"[{attempt_id[:8]}] reserved" in budget_view.stdout
        assert "in-flight" in budget_view.stdout

        # Manual reconciliation or release of the live reservation must be refused.
        for extra_flag in ("--release", "--settle"):
            rec_refused = _run_cli(
                [
                    "budget",
                    "reconcile",
                    attempt_id[:8],
                    extra_flag,
                    "--data-dir",
                    str(data_dir),
                ],
                env=env,
                cwd=tmp_path,
            )
            assert rec_refused.returncode == 2
            assert "currently reserved (in flight)" in rec_refused.stderr

        # SQLite state remains reserved (SQLite TX is closed during pause).
        conn = sqlite3.connect(str(data_dir / DB_FILENAME), timeout=2.0)
        try:
            row = conn.execute(
                "SELECT state, held_micro_usd, settled_micro_usd FROM gateway_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            assert row == ("reserved", reserved, 0)
        finally:
            conn.close()

        assert len(read_file_transport_calls(counter_path)) == expected_mid_calls
    finally:
        resume_path.write_text("go\n", encoding="utf-8")

    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 0, err
    assert err == ""
    result = json.loads(out.strip())
    assert result["status"] == "settled"
    assert result["attempt_id"] == attempt_id
    assert result["reserved_micro_usd"] == reserved
    assert result["held_micro_usd"] == 0
    assert result["settled_micro_usd"] > 0

    # Sent exactly once across the entire lifecycle.
    final_calls = read_file_transport_calls(counter_path)
    assert len(final_calls) == 1
    assert final_calls[0].attempt_id == attempt_id

    # Settled exactly once in SQLite and ledger summary.
    storage, ledger, _ = _open_ledger(data_dir)
    try:
        summary = ledger.summary()
        assert summary.settled_count == 1
        assert summary.uncertain_hold_count == 0
        assert summary.held_micro_usd == 0
        assert summary.settled_micro_usd == result["settled_micro_usd"]
    finally:
        storage.close()

    conn = sqlite3.connect(str(data_dir / DB_FILENAME))
    try:
        rcpt_count = conn.execute(
            "SELECT COUNT(*) FROM gateway_receipts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert rcpt_count == (1,)
    finally:
        conn.close()
