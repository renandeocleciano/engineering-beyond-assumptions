import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from experiments.experiment_001.phase2 import run_phase2

pytestmark = pytest.mark.postgres


@pytest.fixture
def engine() -> Iterator[Engine]:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("set DATABASE_URL to a migrated PostgreSQL database")
    engine = create_engine(os.environ["DATABASE_URL"], pool_size=8)
    yield engine
    engine.dispose()


def test_phase2_separates_logical_work_from_database_attempts(engine: Engine) -> None:
    result = run_phase2(
        engine,
        workers=4,
        warmup_iterations=5,
        iterations=25,
        counters=10,
        strategy="atomic",
        isolation="READ COMMITTED",
        max_retries=0,
        base_delay_s=0.0,
        max_delay_s=0.0,
        sample_interval_s=1.0,
    )
    assert result["logical_operations"] == 100
    assert result["database_attempts"] == 100
    assert result["retries"] == 0
    assert result["exhausted_retries"] == 0
    assert result["other_failures"] == 0
    assert result["committed"] == 100
    assert result["final_value"] == 100
    assert result["lost_updates"] == 0
    assert result["counter_values"] == [9, 10, 11, 12, 12, 11, 10, 9, 8, 8]
    assert result["latency_ms_p50"] is not None
    assert result["latency_ms_p95"] is not None
    assert result["latency_ms_p99"] is not None
    throughput = result["successful_logical_operations_per_second"]
    elapsed = result["elapsed_seconds"]
    assert isinstance(throughput, float)
    assert isinstance(elapsed, float)
    assert throughput == result["committed"] / elapsed
    with engine.connect() as conn:
        leftover = conn.scalar(text("SELECT count(*) FROM counters WHERE name LIKE 'bench2-%'"))
    assert leftover == 0


def test_phase2_serializable_retries_keep_atomic_commits(engine: Engine) -> None:
    result = run_phase2(
        engine,
        workers=4,
        warmup_iterations=1,
        iterations=8,
        counters=1,
        strategy="atomic",
        isolation="SERIALIZABLE",
        max_retries=6,
        base_delay_s=0.0,
        max_delay_s=0.0,
        sample_interval_s=1.0,
    )
    logical = result["logical_operations"]
    assert logical == 32
    assert result["committed"] + result["exhausted_retries"] + result["other_failures"] == logical
    assert result["database_attempts"] == logical + result["retries"]
    assert result["final_value"] == result["committed"]
    assert result["lost_updates"] == 0
    assert isinstance(result["max_lock_waits"], int)
    assert isinstance(result["max_ungranted_locks"], int)
    assert result["latency_ms_p99"] is None


def test_phase2_read_modify_write_baseline_can_lose_updates(engine: Engine) -> None:
    result = run_phase2(
        engine,
        workers=4,
        warmup_iterations=0,
        iterations=20,
        counters=1,
        strategy="read-modify-write",
        isolation="READ COMMITTED",
        max_retries=0,
        base_delay_s=0.0,
        max_delay_s=0.0,
        sample_interval_s=1.0,
    )
    assert result["committed"] == 80
    assert result["retries"] == 0
    assert result["lost_updates"] == result["committed"] - result["final_value"]
    assert result["lost_updates"] > 0
