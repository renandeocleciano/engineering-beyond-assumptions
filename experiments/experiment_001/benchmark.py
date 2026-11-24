"""Measure concurrent increments; results describe only the environment used to run them."""

import argparse
import json
import platform
import statistics
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from app.config import Settings


def _order_statistic_ms(sorted_latencies: list[float], fraction: float) -> float:
    """Inclusive nearest rank: index floor((n - 1) * fraction) of an ascending sample."""
    return sorted_latencies[int((len(sorted_latencies) - 1) * fraction)] * 1000


def _deadlock_count(engine: Engine) -> int:
    with engine.connect() as conn:
        count = conn.scalar(
            text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
        )
    return int(count or 0)


def _sqlstate(exc: DBAPIError) -> str:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return sqlstate if isinstance(sqlstate, str) else "unknown"


def run(
    engine: Engine, workers: int, iterations: int, strategy: str, isolation: str
) -> dict[str, object]:
    if workers < 1 or iterations < 1:
        raise ValueError("workers and iterations must be positive")
    if strategy not in {"atomic", "read-modify-write"}:
        raise ValueError("unknown strategy")
    if isolation not in {"READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"}:
        raise ValueError("unknown isolation")
    name = f"bench-{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO counters (name, value) VALUES (:name, 0)"), {"name": name})
        version = conn.scalar(text("SHOW server_version"))
    barrier = Barrier(workers)

    def worker() -> tuple[list[float], int, int, Counter[str]]:
        latencies: list[float] = []
        committed = failures = 0
        errors: Counter[str] = Counter()
        # Check out a connection before the barrier so pool limits cannot deadlock the run.
        with engine.connect().execution_options(isolation_level=isolation) as conn:
            barrier.wait(timeout=30)
            for _ in range(iterations):
                start = time.perf_counter()
                try:
                    with conn.begin():
                        if strategy == "atomic":
                            conn.execute(
                                text("UPDATE counters SET value = value + 1 WHERE name = :n"),
                                {"n": name},
                            )
                        else:
                            value = conn.scalar(
                                text("SELECT value FROM counters WHERE name = :n"), {"n": name}
                            )
                            conn.execute(
                                text("UPDATE counters SET value = :v WHERE name = :n"),
                                {"n": name, "v": value + 1},
                            )
                    committed += 1
                except DBAPIError as exc:
                    # No retries: failures are part of the measured outcome.
                    failures += 1
                    errors[_sqlstate(exc)] += 1
                latencies.append(time.perf_counter() - start)
        return latencies, committed, failures, errors

    try:
        deadlocks_before = _deadlock_count(engine)
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda _: worker(), range(workers)))
        elapsed = time.perf_counter() - start
        deadlocks = _deadlock_count(engine) - deadlocks_before
        with engine.connect() as conn:
            final = conn.scalar(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
        latencies = sorted(sample for result, _, _, _ in results for sample in result)
        committed = sum(count for _, count, _, _ in results)
        failed = sum(count for _, _, count, _ in results)
        errors: Counter[str] = Counter()
        for _, _, _, worker_errors in results:
            errors.update(worker_errors)
        attempted = workers * iterations
        return {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "postgresql": version,
            "workers": workers,
            "iterations_per_worker": iterations,
            "strategy": strategy,
            "isolation": isolation,
            "attempted": attempted,
            "committed": committed,
            "failed": failed,
            "errors_by_sqlstate": dict(sorted(errors.items())),
            "final_value": final,
            "lost_updates": committed - final,
            "deadlocks": deadlocks,
            "elapsed_seconds": elapsed,
            "commits_per_second": committed / elapsed,
            "latency_ms_p50": statistics.median(latencies) * 1000,
            "latency_ms_p95": _order_statistic_ms(latencies, 0.95),
            # p99 is one order statistic. Below 100 attempts it is too coarse to report.
            "latency_ms_p99": _order_statistic_ms(latencies, 0.99) if attempted >= 100 else None,
        }
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM counters WHERE name = :n"), {"n": name})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--strategy", choices=["atomic", "read-modify-write"], default="atomic")
    parser.add_argument(
        "--isolation",
        choices=["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"],
        default="READ COMMITTED",
    )
    args = parser.parse_args()
    engine = create_engine(Settings().database_url, pool_size=max(args.workers, 1), max_overflow=0)
    try:
        print(
            json.dumps(
                run(engine, args.workers, args.iterations, args.strategy, args.isolation), indent=2
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
