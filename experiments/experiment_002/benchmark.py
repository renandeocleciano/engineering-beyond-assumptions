"""Compare transfer strategies. A violated invariant is a failed strategy, not a faster one.

`pessimistic_sql` is the raw-SQL lock strategy. `pessimistic_service` calls
`execute_transfer` in a new session per operation, which is the public path.
The original recorded files labeled the SQL strategy `pessimistic`.
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Barrier
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import Settings
from app.ledger.errors import InsufficientFunds
from app.ledger.reconcile import invariant_report, violation_count
from app.ledger.service import create_customer_account, execute_transfer, fund_account
from experiments.experiment_001.benchmark import _order_statistic_ms
from experiments.experiment_002.strategies import StrategyResult, run_strategy

_SQL_STRATEGIES = {
    "naive": "naive",
    "conditional": "conditional",
    "pessimistic_sql": "pessimistic",
    "optimistic": "optimistic",
}
_STRATEGIES = (*_SQL_STRATEGIES, "pessimistic_service")


@dataclass
class _Window:
    success_latencies: list[float] = field(default_factory=list)
    e2e_latencies: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)
    committed: int = 0
    rejected: int = 0
    retries: int = 0
    exhausted: int = 0
    attempts: int = 0

    def add(self, outcome: StrategyResult, elapsed: float) -> None:
        self.e2e_latencies.append(elapsed)
        self.attempts += outcome.attempts
        self.retries += outcome.retries
        self.exhausted += int(outcome.exhausted)
        self.committed += int(outcome.committed)
        self.rejected += int(outcome.rejected)
        if outcome.committed:
            self.success_latencies.append(elapsed)
        if outcome.sqlstate is not None:
            self.errors[outcome.sqlstate] += 1


def run_hot_source(
    engine: Engine,
    *,
    strategy: str,
    workers: int,
    warmup_iterations: int,
    iterations: int,
    amount_minor: int,
) -> dict[str, object]:
    """One funded source, one destination per worker, enough funds for every measured transfer."""
    _validate(strategy, workers, warmup_iterations, iterations, amount_minor, even_workers=False)
    _clear(engine)
    with Session(engine) as session, session.begin():
        source = create_customer_account(session, "USD")
        destinations = [create_customer_account(session, "USD") for _ in range(workers)]
        needed = amount_minor * workers * (warmup_iterations + iterations)
        fund_account(session, source.id, needed)
        pairs = [(source.id, account.id) for account in destinations]
    return _execute(
        engine,
        scenario="hot-source",
        strategy=strategy,
        pairs=pairs,
        workers=workers,
        warmup_iterations=warmup_iterations,
        iterations=iterations,
        amount_minor=amount_minor,
        lock_timeout=None,
    )


def run_opposite(
    engine: Engine,
    *,
    strategy: str,
    workers: int,
    warmup_iterations: int,
    iterations: int,
    amount_minor: int,
) -> dict[str, object]:
    """Half the workers transfer A to B and half transfer B to A, with funds for every transfer."""
    _validate(strategy, workers, warmup_iterations, iterations, amount_minor, even_workers=True)
    _clear(engine)
    with Session(engine) as session, session.begin():
        first = create_customer_account(session, "USD")
        second = create_customer_account(session, "USD")
        needed = amount_minor * (workers // 2) * (warmup_iterations + iterations)
        fund_account(session, first.id, needed)
        fund_account(session, second.id, needed)
        pairs = [
            (first.id, second.id) if index % 2 == 0 else (second.id, first.id)
            for index in range(workers)
        ]
    return _execute(
        engine,
        scenario="opposite",
        strategy=strategy,
        pairs=pairs,
        workers=workers,
        warmup_iterations=warmup_iterations,
        iterations=iterations,
        amount_minor=amount_minor,
        lock_timeout="2s",
    )


def _execute(
    engine: Engine,
    *,
    scenario: str,
    strategy: str,
    pairs: Sequence[tuple[UUID, UUID]],
    workers: int,
    warmup_iterations: int,
    iterations: int,
    amount_minor: int,
    lock_timeout: str | None,
) -> dict[str, object]:
    environment = _environment(engine)
    deadlocks_before = _deadlocks(engine)
    marker: dict[str, float | int] = {}

    def mark() -> None:
        marker["deadlocks"] = _deadlocks(engine)
        marker["started"] = time.perf_counter()

    start_barrier = Barrier(workers)
    measure_barrier = Barrier(workers, action=mark)

    def worker(index: int) -> tuple[_Window, _Window]:
        warmup = _Window()
        measured = _Window()
        source_id, destination_id = pairs[index]
        connection: Connection | None = None

        def one(window: _Window) -> None:
            begin = time.perf_counter()
            outcome = _invoke(
                engine,
                connection,
                strategy,
                source_id,
                destination_id,
                amount_minor,
                lock_timeout,
            )
            window.add(outcome, time.perf_counter() - begin)

        if strategy == "pessimistic_service":
            start_barrier.wait(120)
            for _ in range(warmup_iterations):
                one(warmup)
            measure_barrier.wait(120)
            for _ in range(iterations):
                one(measured)
            return warmup, measured
        with engine.connect() as held:
            connection = held
            if lock_timeout is not None:
                held.execute(text("SET lock_timeout = '2s'"))
                held.commit()
            start_barrier.wait(120)
            for _ in range(warmup_iterations):
                one(warmup)
            measure_barrier.wait(120)
            for _ in range(iterations):
                one(measured)
        return warmup, measured

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(worker, range(workers)))
    elapsed = time.perf_counter() - float(marker["started"])
    deadlocks_after_warmup = int(marker["deadlocks"])
    deadlocks_after = _deadlocks(engine)
    warmup = _merge(window for window, _measured in results)
    measured = _merge(_measured for _warmup, _measured in results)
    with engine.connect() as conn:
        report = invariant_report(conn)
    logical = workers * iterations
    return {
        "phase": "experiment-002-corrected",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "host_cpu_count": os.cpu_count(),
        **environment,
        "scenario": scenario,
        "strategy": strategy,
        "workers": workers,
        "warmup_iterations_per_worker": warmup_iterations,
        "iterations_per_worker": iterations,
        "amount_minor": amount_minor,
        "lock_timeout": lock_timeout,
        "logical_operations": logical,
        "database_attempts": measured.attempts,
        "successful_transfers": measured.committed,
        "rejected_transfers": measured.rejected,
        "retries": measured.retries,
        "exhausted_retries": measured.exhausted,
        "deadlock_errors": measured.errors.get("40P01", 0),
        "lock_timeouts": measured.errors.get("55P03", 0),
        "errors_by_sqlstate": dict(sorted(measured.errors.items())),
        "deadlocks": deadlocks_after - deadlocks_after_warmup,
        "elapsed_seconds": elapsed,
        "successful_transfers_per_second": _rate(measured.committed, elapsed),
        "logical_operations_per_second": _rate(logical, elapsed),
        "success_latency_ms": _latency(measured.success_latencies),
        "e2e_latency_ms": _latency(measured.e2e_latencies),
        "warmup": {
            "logical_operations": workers * warmup_iterations,
            "database_attempts": warmup.attempts,
            "successful_transfers": warmup.committed,
            "rejected_transfers": warmup.rejected,
            "retries": warmup.retries,
            "exhausted_retries": warmup.exhausted,
            "deadlock_errors": warmup.errors.get("40P01", 0),
            "lock_timeouts": warmup.errors.get("55P03", 0),
            "errors_by_sqlstate": dict(sorted(warmup.errors.items())),
            "deadlocks": deadlocks_after_warmup - deadlocks_before,
        },
        "invariant_report": report,
        "invariant_violations": violation_count(report),
    }


def _invoke(
    engine: Engine,
    connection: Connection | None,
    strategy: str,
    source_id: object,
    destination_id: object,
    amount_minor: int,
    lock_timeout: str | None,
) -> StrategyResult:
    if strategy == "pessimistic_service":
        return _service_transfer(engine, source_id, destination_id, amount_minor, lock_timeout)
    if connection is None:
        raise RuntimeError("SQL strategy requires a connection")
    return run_strategy(
        connection,
        _SQL_STRATEGIES[strategy],
        UUID(str(source_id)),
        UUID(str(destination_id)),
        amount_minor,
    )


def _service_transfer(
    engine: Engine,
    source_id: object,
    destination_id: object,
    amount_minor: int,
    lock_timeout: str | None,
) -> StrategyResult:
    try:
        with Session(engine) as session, session.begin():
            if lock_timeout is not None:
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
            execute_transfer(
                session,
                UUID(str(source_id)),
                UUID(str(destination_id)),
                amount_minor,
                "USD",
            )
    except InsufficientFunds:
        return StrategyResult(False, True, 1, 0, False, None)
    except DBAPIError as exc:
        return StrategyResult(False, False, 1, 0, False, _sqlstate(exc))
    return StrategyResult(True, False, 1, 0, False, None)


def _merge(windows: Iterable[_Window]) -> _Window:
    merged = _Window()
    for window in windows:
        merged.success_latencies.extend(window.success_latencies)
        merged.e2e_latencies.extend(window.e2e_latencies)
        merged.errors.update(window.errors)
        merged.committed += window.committed
        merged.rejected += window.rejected
        merged.retries += window.retries
        merged.exhausted += window.exhausted
        merged.attempts += window.attempts
    return merged


def _latency(samples: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50": None if not ordered else statistics.median(ordered) * 1000,
        "p95": None if not ordered else _order_statistic_ms(ordered, 0.95),
        "p99": None if len(ordered) < 100 else _order_statistic_ms(ordered, 0.99),
    }


def _validate(
    strategy: str,
    workers: int,
    warmup_iterations: int,
    iterations: int,
    amount_minor: int,
    *,
    even_workers: bool,
) -> None:
    invalid_workers = workers < 1 or (even_workers and (workers < 2 or workers % 2 != 0))
    if strategy not in _STRATEGIES or invalid_workers or iterations < 1 or amount_minor < 1:
        raise ValueError("invalid workload")
    if warmup_iterations < 0:
        raise ValueError("warmup must be zero or positive")


def _rate(count: int, elapsed: float) -> float | None:
    if elapsed <= 0:
        return None
    return count / elapsed


def _clear(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entries, fundings, transfers, accounts"))


def _deadlocks(engine: Engine) -> int:
    with engine.connect() as conn:
        count = conn.scalar(
            text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
        )
    return int(count or 0)


def _environment(engine: Engine) -> dict[str, object]:
    with engine.connect() as conn:
        return {
            "postgresql": conn.scalar(text("SHOW server_version")),
            "isolation": conn.scalar(text("SHOW default_transaction_isolation")),
            "deadlock_timeout": conn.scalar(text("SHOW deadlock_timeout")),
        }


def _sqlstate(exc: DBAPIError) -> str:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return sqlstate if isinstance(sqlstate, str) else "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("hot-source", "opposite"), default="hot-source")
    parser.add_argument("--strategy", choices=_STRATEGIES, default="pessimistic_service")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--warmup-iterations", type=int, default=10)
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--amount-minor", type=int, default=100)
    args = parser.parse_args()
    engine = create_engine(Settings().database_url, pool_size=args.workers + 2, max_overflow=0)
    try:
        runner = run_hot_source if args.scenario == "hot-source" else run_opposite
        result = runner(
            engine,
            strategy=args.strategy,
            workers=args.workers,
            warmup_iterations=args.warmup_iterations,
            iterations=args.iterations,
            amount_minor=args.amount_minor,
        )
    finally:
        engine.dispose()
    print(json.dumps(result, indent=2))
    if result["invariant_violations"] != 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
