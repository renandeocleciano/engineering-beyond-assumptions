"""Benchmark idempotent vs unsafe transfer retry strategies for Experiment 003.

Exits nonzero when the protected (safe) strategy produces duplicate financial
effects or when financial invariants are violated. Unsafe strategies may report
duplicates as expected experimental findings.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.laboratory import require_laboratory_database_url, truncate_financial_tables
from app.ledger.errors import IdempotencyKeyConflict, InsufficientFunds
from app.ledger.faults import clear_faults, maybe_fault, set_fault
from app.ledger.idempotency import execute_idempotent_transfer
from app.ledger.reconcile import violations
from app.ledger.service import create_customer_account, fund_account
from experiments.experiment_001.benchmark import _order_statistic_ms
from experiments.experiment_003.unsafe import (
    ensure_unsafe_table,
    naive_check_then_insert,
    retry_without_idempotency,
)

_STRATEGIES = ("safe", "unsafe_retry", "naive_check")
_SCENARIOS = (
    "baseline",
    "sequential_retries",
    "concurrent_duplicate_storm",
    "mixed_traffic",
    "failure_recovery",
)


@dataclass
class _Window:
    request_latencies: list[float] = field(default_factory=list)
    logical_latencies: list[float] = field(default_factory=list)
    initial_latencies: list[float] = field(default_factory=list)
    retry_latencies: list[float] = field(default_factory=list)
    http_or_service_attempts: int = 0
    logical_operations: int = 0
    committed_transfers: int = 0
    duplicate_effects: int = 0
    deduplicated: int = 0
    conflicts: int = 0
    rollbacks: int = 0
    recovered_lost_responses: int = 0
    transport_timeouts: int = 0
    retry_counts: int = 0
    retry_exhausted: int = 0
    errors: Counter[str] = field(default_factory=Counter)


def _git_meta() -> tuple[str, bool]:
    root = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
    )
    return sha, dirty


def _percentile(samples: list[float], fraction: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return round(_order_statistic_ms(ordered, fraction), 3)


def _clear(engine: Engine) -> None:
    require_laboratory_database_url()
    truncate_financial_tables(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS unsafe_idempotency_attempts"))


def _seed_pair(engine: Engine, amount_minor: int, funding: int) -> tuple[UUID, UUID]:
    with Session(engine) as session, session.begin():
        source = create_customer_account(session, "USD")
        destination = create_customer_account(session, "USD")
        fund_account(session, source.id, funding)
        return source.id, destination.id


def _attempt_safe(
    engine: Engine,
    *,
    key: str,
    source: UUID,
    destination: UUID,
    amount_minor: int,
) -> tuple[str, float, UUID | None]:
    started = time.perf_counter()
    try:
        with Session(engine) as session, session.begin():
            outcome = execute_idempotent_transfer(
                session,
                idempotency_key=key,
                source_account_id=source,
                destination_account_id=destination,
                amount_minor=amount_minor,
                currency="USD",
            )
            transfer_id = outcome.transfer.id
            kind = "replay" if outcome.replay else "created"
        # Mirrors the API: fault after COMMIT, before the client observes success.
        maybe_fault("after_commit_before_response")
        return kind, time.perf_counter() - started, transfer_id
    except IdempotencyKeyConflict:
        return "conflict", time.perf_counter() - started, None
    except InsufficientFunds:
        return "insufficient", time.perf_counter() - started, None
    except Exception as exc:  # noqa: BLE001 — classified for experiment metrics
        return f"error:{type(exc).__name__}", time.perf_counter() - started, None


def _attempt_unsafe_retry(
    engine: Engine,
    *,
    source: UUID,
    destination: UUID,
    amount_minor: int,
) -> tuple[str, float, UUID | None]:
    started = time.perf_counter()
    try:
        with Session(engine) as session, session.begin():
            outcome = retry_without_idempotency(
                session,
                source_account_id=source,
                destination_account_id=destination,
                amount_minor=amount_minor,
                currency="USD",
            )
            return "created", time.perf_counter() - started, outcome.transfer_id
    except InsufficientFunds:
        return "insufficient", time.perf_counter() - started, None
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}", time.perf_counter() - started, None


def _attempt_naive(
    engine: Engine,
    *,
    key: str,
    source: UUID,
    destination: UUID,
    amount_minor: int,
) -> tuple[str, float, UUID | None]:
    started = time.perf_counter()
    try:
        with Session(engine) as session, session.begin():
            outcome = naive_check_then_insert(
                session,
                scope="lab.unsafe",
                key=key,
                source_account_id=source,
                destination_account_id=destination,
                amount_minor=amount_minor,
                currency="USD",
            )
            kind = "created" if outcome.created else "replay"
            return kind, time.perf_counter() - started, outcome.transfer_id
    except InsufficientFunds:
        return "insufficient", time.perf_counter() - started, None
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}", time.perf_counter() - started, None


def _dispatch(
    strategy: str,
    engine: Engine,
    *,
    key: str,
    source: UUID,
    destination: UUID,
    amount_minor: int,
) -> tuple[str, float, UUID | None]:
    if strategy == "safe":
        return _attempt_safe(
            engine, key=key, source=source, destination=destination, amount_minor=amount_minor
        )
    if strategy == "unsafe_retry":
        return _attempt_unsafe_retry(
            engine, source=source, destination=destination, amount_minor=amount_minor
        )
    return _attempt_naive(
        engine, key=key, source=source, destination=destination, amount_minor=amount_minor
    )


def _full_jitter_sleep(attempt: int, base_ms: float, cap_ms: float, rng: random.Random) -> None:
    ceiling = min(cap_ms, base_ms * (2**attempt))
    delay = rng.uniform(0.0, ceiling) / 1000.0
    if delay > 0:
        time.sleep(delay)


def run_scenario(
    engine: Engine,
    *,
    scenario: str,
    strategy: str,
    workers: int,
    warmup: int,
    iterations: int,
    amount_minor: int,
    duplicate_rate: float,
    retry_budget: int,
    backoff_base_ms: float,
    backoff_cap_ms: float,
    seed: int,
    inject_lost_response_rate: float,
) -> dict[str, object]:
    if strategy not in _STRATEGIES:
        raise ValueError(f"unknown strategy {strategy}")
    if scenario not in _SCENARIOS:
        raise ValueError(f"unknown scenario {scenario}")
    if not 0 <= duplicate_rate <= 1:
        raise ValueError("duplicate_rate must be in [0, 1]")
    clear_faults()
    _clear(engine)
    rng = random.Random(seed)
    window = _Window()
    sha, dirty = _git_meta()
    started_at = datetime.now(UTC)

    if scenario == "concurrent_duplicate_storm":
        result = _run_storm(
            engine,
            strategy=strategy,
            workers=workers,
            amount_minor=amount_minor,
            window=window,
        )
    elif scenario == "failure_recovery":
        result = _run_failure_recovery(
            engine,
            strategy=strategy,
            iterations=iterations,
            amount_minor=amount_minor,
            window=window,
            seed=seed,
        )
    else:
        result = _run_traffic(
            engine,
            scenario=scenario,
            strategy=strategy,
            workers=workers,
            warmup=warmup,
            iterations=iterations,
            amount_minor=amount_minor,
            duplicate_rate=duplicate_rate,
            retry_budget=retry_budget,
            backoff_base_ms=backoff_base_ms,
            backoff_cap_ms=backoff_cap_ms,
            rng=rng,
            inject_lost_response_rate=inject_lost_response_rate,
            window=window,
        )

    ended_at = datetime.now(UTC)
    with engine.connect() as conn:
        transfer_count = int(conn.scalar(text("SELECT count(*) FROM transfers")) or 0)
        record_count = int(conn.scalar(text("SELECT count(*) FROM idempotency_records")) or 0)
        invariant_violations = violations(conn)
        pg_version = conn.scalar(text("SHOW server_version"))
    window.committed_transfers = transfer_count
    if strategy == "safe":
        # One completed idempotency record per protected transfer. Warmup transfers
        # are included in both counts and must not look like duplicates.
        window.duplicate_effects = max(0, transfer_count - record_count)
    elif scenario == "concurrent_duplicate_storm":
        window.duplicate_effects = max(0, transfer_count - 1) if transfer_count else 0
    # else: keep duplicates counted during retry traffic / failure recovery

    elapsed = max(sum(window.request_latencies), 1e-9)
    payload: dict[str, object] = {
        "experiment_id": "003",
        "git_commit_sha": sha,
        "working_tree_dirty": dirty,
        "postgresql_version": pg_version,
        "python_version": platform.python_version(),
        "sqlalchemy_version": __import__("sqlalchemy").__version__,
        "isolation_level": "READ COMMITTED",
        "host": {
            "system": platform.system(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
        "scenario": scenario,
        "strategy": strategy,
        "workers": workers,
        "warmup_iterations": warmup,
        "measured_iterations": iterations,
        "retry_policy": "bounded_full_jitter" if retry_budget else "none",
        "retry_budget": retry_budget,
        "backoff_base_ms": backoff_base_ms,
        "backoff_cap_ms": backoff_cap_ms,
        "random_seed": seed,
        "duplicate_rate": duplicate_rate,
        "inject_lost_response_rate": inject_lost_response_rate,
        "amount_minor": amount_minor,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "correctness": {
            "logical_operations": window.logical_operations,
            "service_attempts": window.http_or_service_attempts,
            "committed_transfers": window.committed_transfers,
            "duplicate_transfer_effects": window.duplicate_effects,
            "deduplicated_requests": window.deduplicated,
            "conflicting_key_requests": window.conflicts,
            "rollbacks": window.rollbacks,
            "recovered_lost_responses": window.recovered_lost_responses,
            "invariant_violations": invariant_violations,
            "unique_logical_successes": result["unique_logical_successes"],
        },
        "measurement": {
            "schema_version": 2,
            "warmup_iterations": warmup,
            "measured_iterations": iterations,
            "workers": workers,
            # Traffic scenarios execute warmup transfers before the measured window.
            # Latency and attempt counters below exclude warmup; committed_transfers
            # is a final table count and therefore includes warmup rows when warmup>0.
            "attempt_and_latency_metrics_exclude_warmup": scenario
            in {"baseline", "sequential_retries", "mixed_traffic"},
            "committed_transfers_include_warmup_rows": warmup > 0
            and scenario in {"baseline", "sequential_retries", "mixed_traffic"},
            "service_level_not_http": True,
            "recorded_runs": 1,
            "throughput_notes": {
                "service_attempts_per_second": (
                    "Measured service-call attempts per second (not HTTP)."
                ),
                "committed_transfer_rows_per_second": (
                    "All transfer rows / elapsed, including duplicates and warmup rows."
                ),
                "unique_logical_successes_per_second": (
                    "Measured-window unique logical successes / elapsed."
                ),
            },
        },
        "performance": {
            "service_attempts_per_second": round(
                window.http_or_service_attempts / elapsed, 3
            ),
            # Kept for readers of v1 JSON; identical to service_attempts_per_second.
            "requests_per_second": round(window.http_or_service_attempts / elapsed, 3),
            "committed_transfer_rows_per_second": round(
                window.committed_transfers / elapsed, 3
            ),
            "unique_logical_successes_per_second": round(
                int(result["unique_logical_successes"]) / elapsed, 3
            ),
            "latency_ms_p50": _percentile(window.request_latencies, 0.50),
            "latency_ms_p95": _percentile(window.request_latencies, 0.95),
            "latency_ms_p99": _percentile(window.request_latencies, 0.99),
            "logical_latency_ms_p50": _percentile(window.logical_latencies, 0.50),
            "logical_latency_ms_p95": _percentile(window.logical_latencies, 0.95),
            "initial_attempt_latency_ms_p50": _percentile(window.initial_latencies, 0.50),
            "retry_attempt_latency_ms_p50": _percentile(window.retry_latencies, 0.50),
            "mean_request_latency_ms": round(statistics.fmean(window.request_latencies) * 1000, 3)
            if window.request_latencies
            else None,
        },
        "reliability": {
            "transport_timeouts": window.transport_timeouts,
            "retry_counts": window.retry_counts,
            "retry_exhausted": window.retry_exhausted,
            "error_counts": dict(window.errors),
        },
        "expected_duplicates_for_unsafe": strategy != "safe",
    }
    clear_faults()
    return payload


def _run_storm(
    engine: Engine,
    *,
    strategy: str,
    workers: int,
    amount_minor: int,
    window: _Window,
) -> dict[str, int]:
    source, destination = _seed_pair(engine, amount_minor, amount_minor * 10)
    if strategy == "naive_check":
        with Session(engine) as session, session.begin():
            ensure_unsafe_table(session)
    key = f"storm-{uuid4()}"
    start = Barrier(workers)
    after_lookup = Barrier(workers) if strategy == "naive_check" else None
    transfer_ids: list[UUID] = []

    def worker() -> None:
        start.wait(timeout=30)
        started = time.perf_counter()
        if strategy == "naive_check":
            assert after_lookup is not None
            try:
                with Session(engine) as session, session.begin():
                    outcome = naive_check_then_insert(
                        session,
                        scope="lab.unsafe",
                        key=key,
                        source_account_id=source,
                        destination_account_id=destination,
                        amount_minor=amount_minor,
                        currency="USD",
                        after_missed_lookup=lambda: None
                        if after_lookup.wait(timeout=30)
                        else None,
                    )
                kind = "created" if outcome.created else "replay"
                transfer_id: UUID | None = outcome.transfer_id
            except Exception as exc:  # noqa: BLE001
                kind = f"error:{type(exc).__name__}"
                transfer_id = None
            elapsed = time.perf_counter() - started
        else:
            kind, elapsed, transfer_id = _dispatch(
                strategy,
                engine,
                key=key,
                source=source,
                destination=destination,
                amount_minor=amount_minor,
            )
        window.http_or_service_attempts += 1
        window.request_latencies.append(elapsed)
        if kind == "created":
            window.logical_operations += 1
            if transfer_id is not None:
                transfer_ids.append(transfer_id)
        elif kind == "replay":
            window.deduplicated += 1
            window.logical_operations += 1
        elif kind == "conflict":
            window.conflicts += 1
        else:
            window.errors[kind] += 1
            window.rollbacks += 1

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(lambda _: worker(), range(workers)))
    unique = len(set(transfer_ids)) if transfer_ids else 0
    with engine.connect() as conn:
        committed = int(conn.scalar(text("SELECT count(*) FROM transfers")) or 0)
    window.duplicate_effects = max(0, committed - 1) if committed else 0
    window.logical_operations = max(window.logical_operations, 1)
    return {"unique_logical_successes": 1 if committed else 0, "unique_ids": unique}


def _run_failure_recovery(
    engine: Engine,
    *,
    strategy: str,
    iterations: int,
    amount_minor: int,
    window: _Window,
    seed: int,
) -> dict[str, int]:
    source, destination = _seed_pair(engine, amount_minor, amount_minor * (iterations + 5) * 2)
    successes = 0
    for index in range(iterations):
        key = f"failrec-{seed}-{index:04d}"
        window.logical_operations += 1
        if strategy == "safe":
            set_fault(
                "after_commit_before_response",
                lambda: (_ for _ in ()).throw(RuntimeError("lost-response")),
            )
            kind, elapsed, _transfer_id = _attempt_safe(
                engine,
                key=key,
                source=source,
                destination=destination,
                amount_minor=amount_minor,
            )
            clear_faults()
            window.http_or_service_attempts += 1
            window.request_latencies.append(elapsed)
            window.initial_latencies.append(elapsed)
            if kind.startswith("error"):
                window.transport_timeouts += 1
                window.errors[kind] += 1
                # Retry after lost response
                kind2, elapsed2, _ = _attempt_safe(
                    engine,
                    key=key,
                    source=source,
                    destination=destination,
                    amount_minor=amount_minor,
                )
                window.http_or_service_attempts += 1
                window.retry_counts += 1
                window.request_latencies.append(elapsed2)
                window.retry_latencies.append(elapsed2)
                window.logical_latencies.append(elapsed + elapsed2)
                if kind2 == "replay":
                    window.recovered_lost_responses += 1
                    window.deduplicated += 1
                    successes += 1
                elif kind2 == "created":
                    successes += 1
                else:
                    window.errors[kind2] += 1
            elif kind == "created":
                successes += 1
                window.logical_latencies.append(elapsed)
            else:
                window.errors[kind] += 1
        else:
            # Unsafe: first "lost" attempt still commits; retry creates a duplicate.
            kind, elapsed, _ = _attempt_unsafe_retry(
                engine, source=source, destination=destination, amount_minor=amount_minor
            )
            window.http_or_service_attempts += 1
            window.request_latencies.append(elapsed)
            window.transport_timeouts += 1
            kind2, elapsed2, _ = _attempt_unsafe_retry(
                engine, source=source, destination=destination, amount_minor=amount_minor
            )
            window.http_or_service_attempts += 1
            window.retry_counts += 1
            window.request_latencies.append(elapsed2)
            if kind == "created":
                successes += 1
            if kind2 == "created":
                window.duplicate_effects += 1
    return {"unique_logical_successes": successes}


def _run_traffic(
    engine: Engine,
    *,
    scenario: str,
    strategy: str,
    workers: int,
    warmup: int,
    iterations: int,
    amount_minor: int,
    duplicate_rate: float,
    retry_budget: int,
    backoff_base_ms: float,
    backoff_cap_ms: float,
    rng: random.Random,
    inject_lost_response_rate: float,
    window: _Window,
) -> dict[str, int]:
    ops = workers * (warmup + iterations)
    funding = amount_minor * ops * 2
    source, destination = _seed_pair(engine, amount_minor, funding)
    successes = 0
    measured_start = workers * warmup

    def one_logical(op_index: int, measure: bool) -> None:
        nonlocal successes
        key = f"op-{op_index:06d}"
        should_duplicate = scenario in {"sequential_retries", "mixed_traffic"} and (
            scenario == "sequential_retries" or rng.random() < duplicate_rate
        )
        logical_started = time.perf_counter()
        window.logical_operations += 1 if measure else 0

        def once(is_retry: bool) -> tuple[str, float]:
            if (
                strategy == "safe"
                and not is_retry
                and inject_lost_response_rate > 0
                and rng.random() < inject_lost_response_rate
            ):
                set_fault(
                    "after_commit_before_response",
                    lambda: (_ for _ in ()).throw(RuntimeError("lost-response")),
                )
            kind, elapsed, _tid = _dispatch(
                strategy,
                engine,
                key=key,
                source=source,
                destination=destination,
                amount_minor=amount_minor,
            )
            clear_faults()
            if measure:
                window.http_or_service_attempts += 1
                window.request_latencies.append(elapsed)
                if is_retry:
                    window.retry_latencies.append(elapsed)
                else:
                    window.initial_latencies.append(elapsed)
            return kind, elapsed

        kind, _elapsed = once(False)
        attempts = 1
        if kind.startswith("error") and "RuntimeError" in kind:
            if measure:
                window.transport_timeouts += 1
                window.errors[kind] += 1
            recovered = False
            while attempts <= retry_budget:
                if measure:
                    window.retry_counts += 1
                _full_jitter_sleep(attempts - 1, backoff_base_ms, backoff_cap_ms, rng)
                kind, _ = once(True)
                attempts += 1
                if kind in {"replay", "created"}:
                    recovered = True
                    if measure and kind == "replay":
                        window.recovered_lost_responses += 1
                        window.deduplicated += 1
                    break
                if kind == "conflict":
                    if measure:
                        window.conflicts += 1
                    break
            if not recovered and measure and attempts > retry_budget:
                window.retry_exhausted += 1
        elif kind == "created":
            pass
        elif kind == "replay":
            if measure:
                window.deduplicated += 1
        elif kind == "conflict":
            if measure:
                window.conflicts += 1
        elif kind == "insufficient":
            if measure:
                window.rollbacks += 1
                window.errors[kind] += 1
        else:
            if measure:
                window.errors[kind] += 1
                window.rollbacks += 1

        if should_duplicate and kind in {"created", "replay"} or (
            should_duplicate and strategy == "unsafe_retry"
        ):
            # Client retries the same logical operation after a simulated timeout.
            if strategy == "unsafe_retry":
                kind2, elapsed2 = once(True)
                if measure:
                    window.retry_counts += 1
                    if kind2 == "created":
                        window.duplicate_effects += 1
            else:
                kind2, elapsed2 = once(True)
                if measure:
                    window.retry_counts += 1
                    if kind2 == "replay":
                        window.deduplicated += 1
                    elif kind2 == "created":
                        window.duplicate_effects += 1
                    elif kind2 == "conflict":
                        window.conflicts += 1

        if measure:
            window.logical_latencies.append(time.perf_counter() - logical_started)
            if kind in {"created", "replay"} or (
                should_duplicate and strategy == "unsafe_retry"
            ):
                # Count one logical success when at least one attempt created money movement
                # or safely replayed it. Unsafe duplicates still count as one logical intent.
                successes += 1

    # Serial traffic for baseline / sequential / mixed to keep duplicate accounting clear.
    for index in range(ops):
        one_logical(index, measure=index >= measured_start)
    return {"unique_logical_successes": successes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=_SCENARIOS, required=True)
    parser.add_argument("--strategy", choices=_STRATEGIES, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--amount-minor", type=int, default=25)
    parser.add_argument("--duplicate-rate", type=float, default=0.3)
    parser.add_argument("--retry-budget", type=int, default=3)
    parser.add_argument("--backoff-base-ms", type=float, default=1.0)
    parser.add_argument("--backoff-cap-ms", type=float, default=50.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--inject-lost-response-rate", type=float, default=0.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    require_laboratory_database_url()
    engine = create_engine(
        Settings().database_url, pool_pre_ping=True, pool_size=max(8, args.workers)
    )
    payload = run_scenario(
        engine,
        scenario=args.scenario,
        strategy=args.strategy,
        workers=args.workers,
        warmup=args.warmup,
        iterations=args.iterations,
        amount_minor=args.amount_minor,
        duplicate_rate=args.duplicate_rate,
        retry_budget=args.retry_budget,
        backoff_base_ms=args.backoff_base_ms,
        backoff_cap_ms=args.backoff_cap_ms,
        seed=args.seed,
        inject_lost_response_rate=args.inject_lost_response_rate,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload["correctness"], indent=2))
    safe = args.strategy == "safe"
    duplicates = int(payload["correctness"]["duplicate_transfer_effects"])  # type: ignore[index]
    violations = int(payload["correctness"]["invariant_violations"])  # type: ignore[index]
    if violations:
        return 2
    if safe and duplicates:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
