"""Isolated transfer strategies. None of these is the HTTP handler.

The API uses app.ledger.service.execute_transfer, which follows the pessimistic
lock order. These functions exist so a benchmark can compare algorithms on the
same schema. A faster incorrect result is still incorrect.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError


@dataclass(frozen=True)
class StrategyResult:
    committed: bool
    rejected: bool
    attempts: int
    retries: int
    exhausted: bool
    sqlstate: str | None


def run_strategy(
    connection: Connection,
    strategy: str,
    source_id: UUID,
    destination_id: UUID,
    amount_minor: int,
    *,
    max_retries: int = 5,
) -> StrategyResult:
    if strategy == "naive":
        return _once(connection, _naive, source_id, destination_id, amount_minor)
    if strategy == "conditional":
        return _once(connection, _conditional, source_id, destination_id, amount_minor)
    if strategy == "pessimistic":
        return _once(connection, _pessimistic, source_id, destination_id, amount_minor)
    if strategy == "optimistic":
        return _optimistic(connection, source_id, destination_id, amount_minor, max_retries)
    raise ValueError("unknown strategy")


_Operation = Callable[[Connection, UUID, UUID, int], None]


def _sqlstate(exc: DBAPIError) -> str:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return sqlstate if isinstance(sqlstate, str) else "unknown"


def _once(
    connection: Connection,
    operation: _Operation,
    source_id: UUID,
    destination_id: UUID,
    amount: int,
) -> StrategyResult:
    try:
        with connection.begin():
            operation(connection, source_id, destination_id, amount)
    except _Rejected:
        return StrategyResult(False, True, 1, 0, False, None)
    except DBAPIError as exc:
        return StrategyResult(False, False, 1, 0, False, _sqlstate(exc))
    return StrategyResult(True, False, 1, 0, False, None)


def _optimistic(
    connection: Connection, source_id: UUID, destination_id: UUID, amount: int, max_retries: int
) -> StrategyResult:
    retries = 0
    for attempt in range(max_retries + 1):
        try:
            with connection.begin():
                _optimistic_once(connection, source_id, destination_id, amount)
        except _Rejected:
            return StrategyResult(False, True, attempt + 1, retries, False, None)
        except _Conflict:
            if attempt == max_retries:
                return StrategyResult(False, False, attempt + 1, retries, True, None)
            retries += 1
            continue
        except DBAPIError as exc:
            return StrategyResult(False, False, attempt + 1, retries, False, _sqlstate(exc))
        return StrategyResult(True, False, attempt + 1, retries, False, None)
    raise RuntimeError("optimistic loop ended without an outcome")


class _Rejected(Exception):
    pass


class _Conflict(Exception):
    pass


def _naive(connection: Connection, source_id: UUID, destination_id: UUID, amount: int) -> None:
    source_balance = _read_balance(connection, source_id)
    if source_balance < amount:
        raise _Rejected
    destination_balance = _read_balance(connection, destination_id)
    _set_balance(connection, source_id, source_balance - amount)
    _set_balance(connection, destination_id, destination_balance + amount)
    _insert_movement(connection, source_id, destination_id, amount)


def _conditional(
    connection: Connection, source_id: UUID, destination_id: UUID, amount: int
) -> None:
    debited = connection.execute(
        text(
            """
            UPDATE accounts
            SET balance_minor = balance_minor - :amount, version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND kind = 'customer' AND status = 'open' AND balance_minor >= :amount
            """
        ),
        {"id": source_id, "amount": amount},
    )
    if debited.rowcount != 1:
        raise _Rejected
    credited = connection.execute(
        text(
            """
            UPDATE accounts
            SET balance_minor = balance_minor + :amount, version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND kind = 'customer' AND status = 'open'
            """
        ),
        {"id": destination_id, "amount": amount},
    )
    if credited.rowcount != 1:
        raise _Rejected
    _insert_movement(connection, source_id, destination_id, amount)


def _pessimistic(
    connection: Connection, source_id: UUID, destination_id: UUID, amount: int
) -> None:
    locked = [_lock(connection, account_id) for account_id in sorted((source_id, destination_id))]
    balances = {account_id: balance for account_id, balance in locked}
    if balances[source_id] < amount:
        raise _Rejected
    _set_balance(connection, source_id, balances[source_id] - amount)
    _set_balance(connection, destination_id, balances[destination_id] + amount)
    _insert_movement(connection, source_id, destination_id, amount)


def _optimistic_once(
    connection: Connection, source_id: UUID, destination_id: UUID, amount: int
) -> None:
    source_balance, source_version = _read_version(connection, source_id)
    destination_balance, destination_version = _read_version(connection, destination_id)
    if source_balance < amount:
        raise _Rejected
    if not _cas(connection, source_id, source_balance - amount, source_version):
        raise _Conflict
    if not _cas(connection, destination_id, destination_balance + amount, destination_version):
        raise _Conflict
    _insert_movement(connection, source_id, destination_id, amount)


def _read_balance(connection: Connection, account_id: UUID) -> int:
    value = connection.scalar(
        text("SELECT balance_minor FROM accounts WHERE id = :id"), {"id": account_id}
    )
    if not isinstance(value, int):
        raise _Rejected
    return value


def _read_version(connection: Connection, account_id: UUID) -> tuple[int, int]:
    row = connection.execute(
        text("SELECT balance_minor, version FROM accounts WHERE id = :id"), {"id": account_id}
    ).one()
    return int(row.balance_minor), int(row.version)


def _set_balance(connection: Connection, account_id: UUID, balance: int) -> None:
    connection.execute(
        text(
            """
            UPDATE accounts
            SET balance_minor = :balance, version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id
            """
        ),
        {"id": account_id, "balance": balance},
    )


def _cas(connection: Connection, account_id: UUID, balance: int, version: int) -> bool:
    result = connection.execute(
        text(
            """
            UPDATE accounts
            SET balance_minor = :balance, version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND version = :version
            """
        ),
        {"id": account_id, "balance": balance, "version": version},
    )
    return result.rowcount == 1


def _lock(connection: Connection, account_id: UUID) -> tuple[UUID, int]:
    row = connection.execute(
        text("SELECT id, balance_minor FROM accounts WHERE id = :id FOR UPDATE"),
        {"id": account_id},
    ).one()
    return UUID(str(row.id)), int(row.balance_minor)


def _insert_movement(
    connection: Connection, source_id: UUID, destination_id: UUID, amount: int
) -> None:
    transfer_id = uuid4()
    currency = connection.scalar(
        text("SELECT currency FROM accounts WHERE id = :id"), {"id": source_id}
    )
    connection.execute(
        text(
            """
            INSERT INTO transfers (
                id, source_account_id, destination_account_id, amount_minor, currency,
                status, created_at, completed_at
            ) VALUES (
                :id, :source, :destination, :amount, :currency,
                'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": transfer_id,
            "source": source_id,
            "destination": destination_id,
            "amount": amount,
            "currency": currency,
        },
    )
    for account_id, signed in ((source_id, -amount), (destination_id, amount)):
        connection.execute(
            text(
                """
                INSERT INTO ledger_entries (
                    id, account_id, transfer_id, funding_id, amount_minor, currency, created_at
                ) VALUES (
                    :id, :account_id, :transfer_id, NULL, :amount, :currency, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": uuid4(),
                "account_id": account_id,
                "transfer_id": transfer_id,
                "amount": signed,
                "currency": currency,
            },
        )
