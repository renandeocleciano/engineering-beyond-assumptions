import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from app.main import app
from experiments.experiment_001.benchmark import run
from experiments.experiment_001.retry import run_transaction

pytestmark = pytest.mark.postgres


@pytest.fixture
def engine() -> Iterator[Engine]:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("set DATABASE_URL to a migrated PostgreSQL database")
    engine = create_engine(os.environ["DATABASE_URL"], pool_size=12)
    yield engine
    engine.dispose()


def test_api_counter(engine: Engine) -> None:
    name = f"test-{uuid.uuid4()}"
    client = TestClient(app)
    try:
        assert client.post("/counters", json={"name": name}).status_code == 201
        assert client.post("/counters", json={"name": name}).status_code == 409
        assert client.post(f"/counters/{name}/increment").json()["value"] == 1
        assert client.get(f"/counters/{name}").json()["value"] == 1
        assert client.get(f"/counters/{name}-missing").status_code == 404
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM counters WHERE name = :n"), {"n": name})


def test_atomic_concurrent_writes(engine: Engine) -> None:
    result = run(engine, 8, 25, "atomic", "READ COMMITTED")
    assert result["failed"] == 0
    assert result["final_value"] == result["committed"] == 200


def test_read_modify_write_loses_updates_at_read_committed(engine: Engine) -> None:
    name = f"test-{uuid.uuid4()}"
    barrier = Barrier(2)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO counters VALUES (:n, 0)"), {"n": name})

    def worker() -> None:
        with engine.begin() as conn:
            value = conn.scalar(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
            barrier.wait(timeout=10)
            conn.execute(
                text("UPDATE counters SET value = :v WHERE name = :n"), {"n": name, "v": value + 1}
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: worker(), range(2)))
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT value FROM counters WHERE name = :n"), {"n": name}) == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM counters WHERE name = :n"), {"n": name})


def _increment(connection: Connection, name: str) -> None:
    connection.execute(text("UPDATE counters SET value = value + 1 WHERE name = :n"), {"n": name})


def test_retry_repeats_the_whole_transaction_after_serialization_failure(engine: Engine) -> None:
    name = f"test-{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO counters VALUES (:n, 0)"), {"n": name})
    first = engine.connect().execution_options(isolation_level="SERIALIZABLE")
    other = engine.connect().execution_options(isolation_level="SERIALIZABLE")
    attempts: list[int] = []
    try:

        def operation(connection: Connection) -> None:
            attempts.append(len(attempts) + 1)
            connection.execute(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
            if len(attempts) == 1:
                with other.begin():
                    _increment(other, name)
            _increment(connection, name)

        outcome = run_transaction(
            first,
            operation,
            max_retries=2,
            base_delay_s=0.0,
            max_delay_s=0.0,
            sleep=lambda _delay: None,
            uniform=lambda _low, _high: 0.0,
        )
        with engine.connect() as conn:
            final = conn.scalar(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
        assert attempts == [1, 2]
        assert outcome.committed is True
        assert outcome.retries == 1
        assert outcome.exhausted is False
        assert outcome.error_sqlstates == ("40001",)
        assert final == 2
    finally:
        first.close()
        other.close()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM counters WHERE name = :n"), {"n": name})


def test_exhausted_serialization_retries_do_not_commit(engine: Engine) -> None:
    name = f"test-{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO counters VALUES (:n, 0)"), {"n": name})
    first = engine.connect().execution_options(isolation_level="SERIALIZABLE")
    other = engine.connect().execution_options(isolation_level="SERIALIZABLE")
    try:

        def operation(connection: Connection) -> None:
            connection.execute(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
            with other.begin():
                _increment(other, name)
            _increment(connection, name)

        outcome = run_transaction(
            first,
            operation,
            max_retries=1,
            base_delay_s=0.0,
            max_delay_s=0.0,
            sleep=lambda _delay: None,
            uniform=lambda _low, _high: 0.0,
        )
        with engine.connect() as conn:
            final = conn.scalar(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
        assert outcome.committed is False
        assert outcome.exhausted is True
        assert outcome.attempts == 2
        assert outcome.retries == 1
        assert outcome.error_sqlstates == ("40001", "40001")
        assert final == 2
    finally:
        first.close()
        other.close()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM counters WHERE name = :n"), {"n": name})


def test_concurrent_retried_atomic_updates_match_committed_work(engine: Engine) -> None:
    name = f"test-{uuid.uuid4()}"
    workers = 4
    iterations = 8
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO counters VALUES (:n, 0)"), {"n": name})

    def worker() -> tuple[int, int, int, int]:
        committed = retries = exhausted = attempts = 0
        with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
            for _ in range(iterations):
                outcome = run_transaction(
                    connection,
                    lambda current: _increment(current, name),
                    max_retries=6,
                    base_delay_s=0.0,
                    max_delay_s=0.0,
                    sleep=lambda _delay: None,
                    uniform=lambda _low, _high: 0.0,
                )
                attempts += outcome.attempts
                retries += outcome.retries
                committed += int(outcome.committed)
                exhausted += int(outcome.exhausted)
        return committed, retries, exhausted, attempts

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda _: worker(), range(workers)))
        with engine.connect() as conn:
            final = conn.scalar(text("SELECT value FROM counters WHERE name = :n"), {"n": name})
        committed = sum(item[0] for item in results)
        retries = sum(item[1] for item in results)
        exhausted = sum(item[2] for item in results)
        attempts = sum(item[3] for item in results)
        logical = workers * iterations
        assert committed + exhausted == logical
        assert attempts == logical + retries
        assert final == committed
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM counters WHERE name = :n"), {"n": name})
