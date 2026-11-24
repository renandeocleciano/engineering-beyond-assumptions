"""Retry a whole transaction after PostgreSQL SQLSTATE 40001."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import DBAPIError


class Transactional(Protocol):
    def begin(self) -> AbstractContextManager[object]: ...


@dataclass(frozen=True)
class TransactionOutcome:
    committed: bool
    attempts: int
    retries: int
    exhausted: bool
    error_sqlstates: tuple[str, ...]


def _sqlstate(exc: DBAPIError) -> str:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return sqlstate if isinstance(sqlstate, str) else "unknown"


def run_transaction[T: Transactional](
    connection: T,
    operation: Callable[[T], None],
    *,
    max_retries: int,
    base_delay_s: float,
    max_delay_s: float,
    sleep: Callable[[float], None],
    uniform: Callable[[float, float], float],
) -> TransactionOutcome:
    """Run operation inside one transaction, retrying only after SQLSTATE 40001.

    Each attempt starts a new transaction and runs operation from the beginning.
    After a serialization failure, sleep for a full-jitter exponential delay:
    uniform(0, min(max_delay_s, base_delay_s * 2^retry_index)).
    """
    if max_retries < 0:
        raise ValueError("max_retries must be zero or positive")
    if base_delay_s < 0 or max_delay_s < 0:
        raise ValueError("delays must be zero or positive")
    errors: list[str] = []
    for attempt in range(max_retries + 1):
        try:
            with connection.begin():
                operation(connection)
        except DBAPIError as exc:
            sqlstate = _sqlstate(exc)
            errors.append(sqlstate)
            budget_spent = attempt == max_retries
            if sqlstate != "40001" or budget_spent:
                return TransactionOutcome(
                    committed=False,
                    attempts=attempt + 1,
                    retries=attempt,
                    exhausted=sqlstate == "40001" and budget_spent,
                    error_sqlstates=tuple(errors),
                )
            cap = min(max_delay_s, base_delay_s * (2**attempt))
            sleep(0.0 if cap <= 0 else uniform(0.0, cap))
            continue
        return TransactionOutcome(
            committed=True,
            attempts=attempt + 1,
            retries=attempt,
            exhausted=False,
            error_sqlstates=tuple(errors),
        )
    raise RuntimeError("retry loop ended without an outcome")
