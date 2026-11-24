import pytest
from sqlalchemy.exc import OperationalError

from experiments.experiment_001.retry import run_transaction


class _Origin(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Connection:
    def begin(self) -> "_Connection":
        return self

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _failure(sqlstate: str) -> OperationalError:
    return OperationalError("statement", {}, _Origin(sqlstate))


def test_retries_the_complete_transaction_after_serialization_failure() -> None:
    calls: list[int] = []

    def operation(_connection: _Connection) -> None:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise _failure("40001")

    outcome = run_transaction(
        _Connection(),
        operation,
        max_retries=3,
        base_delay_s=0.01,
        max_delay_s=0.05,
        sleep=lambda _delay: None,
        uniform=lambda _low, _high: 0.0,
    )

    assert calls == [1, 2]
    assert outcome.committed is True
    assert outcome.attempts == 2
    assert outcome.retries == 1
    assert outcome.exhausted is False
    assert outcome.error_sqlstates == ("40001",)


def test_does_not_retry_other_sqlstates() -> None:
    calls: list[int] = []

    def operation(_connection: _Connection) -> None:
        calls.append(1)
        raise _failure("23505")

    outcome = run_transaction(
        _Connection(),
        operation,
        max_retries=3,
        base_delay_s=0.01,
        max_delay_s=0.05,
        sleep=lambda _delay: None,
        uniform=lambda _low, _high: 0.0,
    )

    assert calls == [1]
    assert outcome.committed is False
    assert outcome.attempts == 1
    assert outcome.retries == 0
    assert outcome.exhausted is False
    assert outcome.error_sqlstates == ("23505",)


def test_exhausted_retries_stop_after_the_bounded_budget() -> None:
    calls: list[int] = []

    def operation(_connection: _Connection) -> None:
        calls.append(len(calls) + 1)
        raise _failure("40001")

    outcome = run_transaction(
        _Connection(),
        operation,
        max_retries=2,
        base_delay_s=0.01,
        max_delay_s=0.05,
        sleep=lambda _delay: None,
        uniform=lambda _low, _high: 0.0,
    )

    assert calls == [1, 2, 3]
    assert outcome.committed is False
    assert outcome.attempts == 3
    assert outcome.retries == 2
    assert outcome.exhausted is True
    assert outcome.error_sqlstates == ("40001", "40001", "40001")


def test_backoff_uses_exponential_full_jitter() -> None:
    delays: list[float] = []
    bounds: list[tuple[float, float]] = []

    def operation(_connection: _Connection) -> None:
        if len(delays) < 3:
            raise _failure("40001")

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return high / 2

    outcome = run_transaction(
        _Connection(),
        operation,
        max_retries=3,
        base_delay_s=0.01,
        max_delay_s=0.05,
        sleep=delays.append,
        uniform=uniform,
    )

    assert outcome.committed is True
    assert bounds == [(0.0, 0.01), (0.0, 0.02), (0.0, 0.04)]
    assert delays == [0.005, 0.01, 0.02]


def test_backoff_is_capped() -> None:
    bounds: list[float] = []

    def operation(_connection: _Connection) -> None:
        if len(bounds) < 4:
            raise _failure("40001")

    def uniform(low: float, high: float) -> float:
        assert low == 0.0
        bounds.append(high)
        return 0.0

    run_transaction(
        _Connection(),
        operation,
        max_retries=4,
        base_delay_s=0.02,
        max_delay_s=0.05,
        sleep=lambda _delay: None,
        uniform=uniform,
    )

    assert bounds == [0.02, 0.04, 0.05, 0.05]


def test_rejects_a_negative_retry_budget() -> None:
    with pytest.raises(ValueError):
        run_transaction(
            _Connection(),
            lambda _connection: None,
            max_retries=-1,
            base_delay_s=0.0,
            max_delay_s=0.0,
            sleep=lambda _delay: None,
            uniform=lambda _low, _high: 0.0,
        )
