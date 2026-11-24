"""Phase 2 measurements. Phase 1 stays in benchmark.py and does not retry."""

import argparse
import json
import platform
import random
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Barrier, Event, Lock, Thread
from typing import TypedDict

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from app.config import Settings
from experiments.experiment_001.benchmark import _order_statistic_ms
from experiments.experiment_001.retry import run_transaction

_ISOLATION = ("READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE")
_STRATEGIES = ("atomic", "read-modify-write")


class Phase2Result(TypedDict):
    phase: int
    timestamp_utc: str
    python: str
    platform: str
    postgresql: object
    workers: int
    warmup_iterations_per_worker: int
    iterations_per_worker: int
    counters: int
    strategy: str
    isolation: str
    max_retries: int
    base_delay_seconds: float
    max_delay_seconds: float
    sample_interval_seconds: float
    logical_operations: int
    database_attempts: int
    committed: int
    retries: int
    exhausted_retries: int
    other_failures: int
    errors_by_sqlstate: dict[str, int]
    final_value: int
    lost_updates: int
    counter_values: list[int]
    failure_rate: float
    retry_rate: float
    deadlocks: int
    elapsed_seconds: float
    successful_logical_operations_per_second: float | None
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    latency_ms_p99: float | None
    lock_samples: int
    max_lock_waits: int
    max_ungranted_locks: int
    wait_event_samples: dict[str, int]
    log_lock_waits: object
    deadlock_timeout: object


@dataclass
class _Tally:
    latencies: list[float] = field(default_factory=list)
    committed: int = 0
    retries: int = 0
    exhausted: int = 0
    other_failures: int = 0
    attempts: int = 0
    errors: Counter[str] = field(default_factory=Counter)


@dataclass
class _Locks:
    samples: int = 0
    max_lock_waits: int = 0
    max_ungranted_locks: int = 0
    wait_events: Counter[str] = field(default_factory=Counter)


def _check(
    workers: int,
    warmup: int,
    iterations: int,
    counters: int,
    strategy: str,
    isolation: str,
    max_retries: int,
    base_delay_s: float,
    max_delay_s: float,
    sample_interval_s: float,
) -> None:
    if workers < 1 or iterations < 1 or counters < 1:
        raise ValueError("workers, iterations, and counters must be positive")
    if warmup < 0 or max_retries < 0:
        raise ValueError("warmup and max_retries must be zero or positive")
    if base_delay_s < 0 or max_delay_s < 0 or sample_interval_s <= 0:
        raise ValueError("delays must be non-negative and the sample interval positive")
    if strategy not in _STRATEGIES:
        raise ValueError("unknown strategy")
    if isolation not in _ISOLATION:
        raise ValueError("unknown isolation")


def _apply(connection: Connection, name: str, strategy: str) -> None:
    if strategy == "atomic":
        connection.execute(
            text("UPDATE counters SET value = value + 1 WHERE name = :n"), {"n": name}
        )
        return
    value = connection.scalar(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
    if not isinstance(value, int):
        raise RuntimeError(f"counter {name} is missing")
    connection.execute(
        text("UPDATE counters SET value = :v WHERE name = :n"), {"n": name, "v": value + 1}
    )


def _deadlocks(engine: Engine) -> int:
    with engine.connect() as conn:
        count = conn.scalar(
            text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
        )
    return int(count or 0)


def _sample(connection: Connection, locks: _Locks) -> None:
    waiting = connection.scalar(
        text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )
    )
    # Server-wide: transaction locks often have a null database oid.
    ungranted = connection.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
    rows = connection.execute(
        text(
            "SELECT COALESCE(wait_event_type, 'none'), COALESCE(wait_event, 'none'), count(*) "
            "FROM pg_stat_activity WHERE datname = current_database() GROUP BY 1, 2"
        )
    )
    locks.samples += 1
    locks.max_lock_waits = max(locks.max_lock_waits, int(waiting or 0))
    locks.max_ungranted_locks = max(locks.max_ungranted_locks, int(ungranted or 0))
    for kind, event, count in rows:
        if isinstance(count, int):
            locks.wait_events[f"{kind}:{event}"] += count


def _percentile(samples: list[float], fraction: float, minimum: int) -> float | None:
    if len(samples) < minimum:
        return None
    ordered = sorted(samples)
    if fraction == 0.5:
        return float(statistics.median(ordered) * 1000)
    return _order_statistic_ms(ordered, fraction)


def _values(engine: Engine, names: Sequence[str]) -> list[int]:
    found: list[int] = []
    with engine.connect() as conn:
        for name in names:
            raw = conn.scalar(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
            if not isinstance(raw, int):
                raise RuntimeError(f"counter {name} is missing")
            found.append(raw)
    return found


def _run_workers(
    engine: Engine,
    names: Sequence[str],
    *,
    workers: int,
    warmup_iterations: int,
    iterations: int,
    strategy: str,
    isolation: str,
    max_retries: int,
    base_delay_s: float,
    max_delay_s: float,
    sample_interval_s: float,
    sleep: Callable[[float], None],
    uniform: Callable[[float, float], float],
) -> tuple[list[_Tally], float, _Locks, list[int]]:
    baseline: list[int] = []

    def capture_baseline() -> None:
        # Runs inside the barrier before any worker enters the measurement window.
        baseline.extend(_values(engine, names))

    ready = Barrier(workers)
    measure = Barrier(workers, action=capture_baseline)
    started, stop = Event(), Event()
    start_lock, start_box = Lock(), list[float]()
    locks, sampler_error = _Locks(), list[BaseException]()

    def sampler() -> None:
        try:
            with engine.connect() as conn:
                if not started.wait(120):
                    raise TimeoutError("measurement window did not start")
                while not stop.is_set():
                    _sample(conn, locks)
                    stop.wait(sample_interval_s)
        except BaseException as exc:
            sampler_error.append(exc)
            raise

    def worker(index: int) -> _Tally:
        tally = _Tally()
        with engine.connect().execution_options(isolation_level=isolation) as conn:
            ready.wait(120)
            for step in range(warmup_iterations):
                target = names[(index + step) % len(names)]

                def warmup(connection: Connection, target: str = target) -> None:
                    _apply(connection, target, strategy)

                run_transaction(
                    conn,
                    warmup,
                    max_retries=max_retries,
                    base_delay_s=base_delay_s,
                    max_delay_s=max_delay_s,
                    sleep=sleep,
                    uniform=uniform,
                )
            measure.wait(120)
            with start_lock:
                if not start_box:
                    start_box.append(time.perf_counter())
                    started.set()
            for step in range(iterations):
                target = names[(index + step) % len(names)]
                begin = time.perf_counter()

                def measured(connection: Connection, target: str = target) -> None:
                    _apply(connection, target, strategy)

                outcome = run_transaction(
                    conn,
                    measured,
                    max_retries=max_retries,
                    base_delay_s=base_delay_s,
                    max_delay_s=max_delay_s,
                    sleep=sleep,
                    uniform=uniform,
                )
                tally.attempts += outcome.attempts
                tally.retries += outcome.retries
                tally.errors.update(outcome.error_sqlstates)
                if outcome.committed:
                    tally.committed += 1
                    tally.latencies.append(time.perf_counter() - begin)
                elif outcome.exhausted:
                    tally.exhausted += 1
                else:
                    tally.other_failures += 1
        return tally

    sampler_thread = Thread(target=sampler)
    sampler_thread.start()
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            tallies = list(executor.map(worker, range(workers)))
        elapsed = time.perf_counter() - start_box[0]
    finally:
        stop.set()
        started.set()
        sampler_thread.join(timeout=5)
    if sampler_error:
        raise sampler_error[0]
    return tallies, elapsed, locks, baseline


def run_phase2(
    engine: Engine,
    *,
    workers: int,
    warmup_iterations: int,
    iterations: int,
    counters: int,
    strategy: str,
    isolation: str,
    max_retries: int,
    base_delay_s: float,
    max_delay_s: float,
    sample_interval_s: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
    uniform: Callable[[float, float], float] = random.uniform,
) -> Phase2Result:
    """Measure one configured run. Warmup is excluded from the reported totals."""
    _check(
        workers,
        warmup_iterations,
        iterations,
        counters,
        strategy,
        isolation,
        max_retries,
        base_delay_s,
        max_delay_s,
        sample_interval_s,
    )
    prefix = f"bench2-{uuid.uuid4()}"
    names = [f"{prefix}-{index}" for index in range(counters)]
    with engine.begin() as conn:
        for name in names:
            conn.execute(
                text("INSERT INTO counters (name, value) VALUES (:name, 0)"), {"name": name}
            )
        version = conn.scalar(text("SHOW server_version"))
        log_lock_waits = conn.scalar(text("SHOW log_lock_waits"))
        deadlock_timeout = conn.scalar(text("SHOW deadlock_timeout"))
    deadlocks_before = _deadlocks(engine)
    try:
        tallies, elapsed, locks, baseline = _run_workers(
            engine,
            names,
            workers=workers,
            warmup_iterations=warmup_iterations,
            iterations=iterations,
            strategy=strategy,
            isolation=isolation,
            max_retries=max_retries,
            base_delay_s=base_delay_s,
            max_delay_s=max_delay_s,
            sample_interval_s=sample_interval_s,
            sleep=sleep,
            uniform=uniform,
        )
        values = [
            end - start for end, start in zip(_values(engine, names), baseline, strict=True)
        ]
        deadlocks = _deadlocks(engine) - deadlocks_before
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM counters WHERE name LIKE :prefix"), {"prefix": f"{prefix}-%"}
            )
    logical = workers * iterations
    committed = sum(item.committed for item in tallies)
    retries = sum(item.retries for item in tallies)
    exhausted = sum(item.exhausted for item in tallies)
    other_failures = sum(item.other_failures for item in tallies)
    attempts = sum(item.attempts for item in tallies)
    errors: Counter[str] = Counter()
    latencies: list[float] = []
    for item in tallies:
        errors.update(item.errors)
        latencies.extend(item.latencies)
    final_value = sum(values)
    return {
        "phase": 2,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "postgresql": version,
        "workers": workers,
        "warmup_iterations_per_worker": warmup_iterations,
        "iterations_per_worker": iterations,
        "counters": counters,
        "strategy": strategy,
        "isolation": isolation,
        "max_retries": max_retries,
        "base_delay_seconds": base_delay_s,
        "max_delay_seconds": max_delay_s,
        "sample_interval_seconds": sample_interval_s,
        "logical_operations": logical,
        "database_attempts": attempts,
        "committed": committed,
        "retries": retries,
        "exhausted_retries": exhausted,
        "other_failures": other_failures,
        "errors_by_sqlstate": dict(sorted(errors.items())),
        "final_value": final_value,
        "lost_updates": committed - final_value,
        "counter_values": values,
        "failure_rate": (exhausted + other_failures) / logical,
        "retry_rate": retries / logical,
        "deadlocks": deadlocks,
        "elapsed_seconds": elapsed,
        "successful_logical_operations_per_second": None if elapsed <= 0 else committed / elapsed,
        "latency_ms_p50": _percentile(latencies, 0.5, 1),
        "latency_ms_p95": _percentile(latencies, 0.95, 1),
        "latency_ms_p99": _percentile(latencies, 0.99, 100),
        "lock_samples": locks.samples,
        "max_lock_waits": locks.max_lock_waits,
        "max_ungranted_locks": locks.max_ungranted_locks,
        "wait_event_samples": dict(sorted(locks.wait_events.items())),
        "log_lock_waits": log_lock_waits,
        "deadlock_timeout": deadlock_timeout,
    }


def set_log_lock_waits(engine: Engine, enabled: bool) -> None:
    """Toggle server-wide lock-wait logging. Waits shorter than deadlock_timeout stay unlogged."""
    statement = (
        "ALTER SYSTEM SET log_lock_waits TO on" if enabled else "ALTER SYSTEM RESET log_lock_waits"
    )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(statement))
        conn.execute(text("SELECT pg_reload_conf()"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--warmup-iterations", type=int, default=50)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--counters", type=int, default=1)
    parser.add_argument("--strategy", choices=_STRATEGIES, default="atomic")
    parser.add_argument("--isolation", choices=_ISOLATION, default="READ COMMITTED")
    parser.add_argument("--max-retries", type=int, default=0)
    parser.add_argument("--base-delay-seconds", type=float, default=0.001)
    parser.add_argument("--max-delay-seconds", type=float, default=0.05)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.05)
    args = parser.parse_args()
    engine = create_engine(Settings().database_url, pool_size=args.workers + 2, max_overflow=0)
    try:
        print(
            json.dumps(
                run_phase2(
                    engine,
                    workers=args.workers,
                    warmup_iterations=args.warmup_iterations,
                    iterations=args.iterations,
                    counters=args.counters,
                    strategy=args.strategy,
                    isolation=args.isolation,
                    max_retries=args.max_retries,
                    base_delay_s=args.base_delay_seconds,
                    max_delay_s=args.max_delay_seconds,
                    sample_interval_s=args.sample_interval_seconds,
                ),
                indent=2,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
