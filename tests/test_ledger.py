"""PostgreSQL tests for transfer invariants. SQLite is not a substitute."""

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal, engine
from app.ledger.errors import InsufficientFunds
from app.ledger.models import Account
from app.ledger.reconcile import invariant_report, violations
from app.ledger.service import execute_transfer, fund_account
from app.main import app

pytestmark = pytest.mark.postgres


@pytest.fixture
def client() -> Iterator[TestClient]:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("set DATABASE_URL to a migrated PostgreSQL database")
    with TestClient(app) as test_client:
        yield test_client
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entries, fundings, transfers, accounts"))


def _account(client: TestClient, currency: str = "USD") -> str:
    response = client.post("/accounts", json={"currency": currency})
    assert response.status_code == 201
    return str(response.json()["id"])


def _fund(client: TestClient, account_id: str, amount: int) -> None:
    response = client.post(f"/accounts/{account_id}/fund", json={"amount_minor": amount})
    assert response.status_code == 200
    assert response.json()["balance_minor"] == amount


def test_account_fund_transfer_and_entries(client: TestClient) -> None:
    source = _account(client)
    destination = _account(client)
    _fund(client, source, 1050)
    created = client.post(
        "/transfers",
        json={
            "source_account_id": source,
            "destination_account_id": destination,
            "amount_minor": 250,
            "currency": "USD",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "completed"
    assert client.get(f"/transfers/{body['id']}").json()["amount_minor"] == 250
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 800
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 250
    entries = client.get(f"/accounts/{source}/entries").json()
    assert [item["amount_minor"] for item in entries["items"]] == [1050, -250]
    assert client.get(f"/accounts/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/transfers/{uuid.uuid4()}").status_code == 404
    with engine.connect() as conn:
        assert violations(conn) == 0


def test_rejects_invalid_transfer_requests(client: TestClient) -> None:
    source = _account(client)
    other = _account(client, "BRL")
    _fund(client, source, 500)
    same = client.post(
        "/transfers",
        json={
            "source_account_id": source,
            "destination_account_id": source,
            "amount_minor": 100,
            "currency": "USD",
        },
    )
    assert same.status_code == 422
    mismatch = client.post(
        "/transfers",
        json={
            "source_account_id": source,
            "destination_account_id": other,
            "amount_minor": 100,
            "currency": "USD",
        },
    )
    assert mismatch.status_code == 409
    poor = client.post(
        "/transfers",
        json={
            "source_account_id": source,
            "destination_account_id": _account(client),
            "amount_minor": 501,
            "currency": "USD",
        },
    )
    assert poor.status_code == 409
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 500
    with engine.connect() as conn:
        assert violations(conn) == 0
        transfers = conn.scalar(text("SELECT count(*) FROM transfers"))
    assert transfers == 0


def test_concurrent_overspend_allows_exactly_one_transfer(client: TestClient) -> None:
    source = _account(client)
    first = _account(client)
    second = _account(client)
    _fund(client, source, 1000)
    barrier = Barrier(2)

    def move(destination: str) -> int:
        barrier.wait(timeout=10)
        try:
            with SessionLocal() as session, session.begin():
                execute_transfer(session, uuid.UUID(source), uuid.UUID(destination), 800, "USD")
        except InsufficientFunds:
            return 0
        return 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(move, (first, second)))
    assert sorted(results) == [0, 1]
    balances = [
        client.get(f"/accounts/{account_id}").json()["balance_minor"]
        for account_id in (source, first, second)
    ]
    assert balances[0] == 200
    assert sorted(balances[1:]) == [0, 800]
    with engine.connect() as conn:
        assert violations(conn) == 0


def test_parallel_transfers_with_sufficient_funds(client: TestClient) -> None:
    source = _account(client)
    destinations = [_account(client) for _ in range(4)]
    _fund(client, source, 1000)
    barrier = Barrier(4)

    def move(destination: str) -> int:
        barrier.wait(timeout=10)
        with SessionLocal() as session, session.begin():
            execute_transfer(session, uuid.UUID(source), uuid.UUID(destination), 100, "USD")
        return 100

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(move, destinations)) == [100, 100, 100, 100]
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 600
    for destination in destinations:
        assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 100
    with engine.connect() as conn:
        assert violations(conn) == 0


def test_failure_after_debit_rolls_back(client: TestClient) -> None:
    source = _account(client)
    destination = _account(client)
    _fund(client, source, 400)

    def boom() -> None:
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        with SessionLocal() as session, session.begin():
            execute_transfer(
                session,
                uuid.UUID(source),
                uuid.UUID(destination),
                125,
                "USD",
                after_debit=boom,
            )
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 400
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 0
    with engine.connect() as conn:
        assert violations(conn) == 0
        transfers = conn.scalar(text("SELECT count(*) FROM transfers"))
    assert transfers == 0


def test_ledger_entries_are_immutable_and_reconciliation_detects_drift(client: TestClient) -> None:
    account_id = _account(client)
    _fund(client, account_id, 300)
    with engine.connect() as conn:
        entry_id = conn.scalar(text("SELECT id FROM ledger_entries LIMIT 1"))
        with pytest.raises(DBAPIError, match="immutable"):
            conn.execute(
                text("UPDATE ledger_entries SET amount_minor = 1 WHERE id = :id"),
                {"id": entry_id},
            )
        conn.rollback()
        with pytest.raises(DBAPIError, match="immutable"):
            conn.execute(text("DELETE FROM ledger_entries WHERE id = :id"), {"id": entry_id})
        conn.rollback()
    session = SessionLocal()
    try:
        session.begin()
        session.execute(
            text("UPDATE accounts SET balance_minor = balance_minor + 5 WHERE id = :id"),
            {"id": account_id},
        )
        report = invariant_report(session.connection())
        assert report["balance_mismatches"] == 1
        assert violations(session.connection()) >= 1
    finally:
        session.rollback()
        session.close()
    with engine.connect() as conn:
        assert violations(conn) == 0


def test_opposite_transfers_do_not_deadlock(client: TestClient) -> None:
    first = _account(client)
    second = _account(client)
    _fund(client, first, 500)
    _fund(client, second, 500)
    before = _deadlocks()
    barrier = Barrier(2)

    def move(source: str, destination: str) -> None:
        barrier.wait(timeout=10)
        for _ in range(10):
            with SessionLocal() as session, session.begin():
                execute_transfer(session, uuid.UUID(source), uuid.UUID(destination), 10, "USD")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(move, first, second),
            executor.submit(move, second, first),
        ]
        for future in futures:
            future.result(timeout=30)
    assert client.get(f"/accounts/{first}").json()["balance_minor"] == 500
    assert client.get(f"/accounts/{second}").json()["balance_minor"] == 500
    assert _deadlocks() == before
    with engine.connect() as conn:
        assert violations(conn) == 0


def test_concurrent_funding_uses_the_balance_loaded_before_the_lock(client: TestClient) -> None:
    """Both sessions read the balance, then both fund. Neither waits while holding a lock.

    The barriers run before fund_account, so they sit ahead of SELECT FOR UPDATE.
    Before the relative-update fix this committed balance 600 against ledger 700.
    """
    account_id = uuid.UUID(_account(client))
    _fund(client, str(account_id), 500)
    loaded = Barrier(2)
    start = Barrier(2)

    def fund_once() -> None:
        with SessionLocal() as session, session.begin():
            customer = session.get(Account, account_id)
            assert customer is not None
            assert customer.balance_minor == 500
            loaded.wait(10)
            start.wait(10)
            fund_account(session, account_id, 100)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _index: fund_once(), (0, 1)))
    assert client.get(f"/accounts/{account_id}").json()["balance_minor"] == 700
    with engine.connect() as conn:
        assert violations(conn) == 0
        credited = conn.scalar(
            text(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries WHERE account_id = :id"
            ),
            {"id": account_id},
        )
    assert credited == 700


def test_concurrent_funding_and_transfer_keep_one_balance(client: TestClient) -> None:
    source = uuid.UUID(_account(client))
    destination = uuid.UUID(_account(client))
    _fund(client, str(source), 400)
    loaded = Barrier(2)
    start = Barrier(2)

    def fund() -> None:
        with SessionLocal() as session, session.begin():
            customer = session.get(Account, source)
            assert customer is not None
            assert customer.balance_minor == 400
            loaded.wait(10)
            start.wait(10)
            fund_account(session, source, 50)

    def transfer() -> None:
        with SessionLocal() as session, session.begin():
            customer = session.get(Account, source)
            assert customer is not None
            assert customer.balance_minor == 400
            loaded.wait(10)
            start.wait(10)
            execute_transfer(session, source, destination, 100, "USD")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(fund), executor.submit(transfer)]
        for future in futures:
            future.result(timeout=20)
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 350
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 100
    with engine.connect() as conn:
        assert violations(conn) == 0


def test_loaded_balance_cannot_spend_funds_removed_by_another_transaction(
    client: TestClient,
) -> None:
    source = uuid.UUID(_account(client))
    destination = uuid.UUID(_account(client))
    _fund(client, str(source), 100)
    with SessionLocal() as session, session.begin():
        loaded = session.get(Account, source)
        assert loaded is not None
        assert loaded.balance_minor == 100
        with SessionLocal() as other, other.begin():
            execute_transfer(other, source, destination, 60, "USD")
        with pytest.raises(InsufficientFunds):
            execute_transfer(session, source, destination, 50, "USD")
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 40
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 60
    with engine.connect() as conn:
        assert violations(conn) == 0
        transfers = conn.scalar(text("SELECT count(*) FROM transfers"))
    assert transfers == 1


def test_transfer_after_the_account_was_loaded_in_this_session(client: TestClient) -> None:
    source = uuid.UUID(_account(client))
    destination = uuid.UUID(_account(client))
    _fund(client, str(source), 500)
    with SessionLocal() as session, session.begin():
        loaded = session.get(Account, source)
        assert loaded is not None
        assert loaded.balance_minor == 500
        with SessionLocal() as other, other.begin():
            fund_account(other, source, 80)
        execute_transfer(session, source, destination, 30, "USD")
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 550
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 30
    with engine.connect() as conn:
        assert violations(conn) == 0


def test_reconciliation_detects_each_planted_anomaly(client: TestClient) -> None:
    customer = _account(client)
    other = _account(client)
    _fund(client, customer, 300)
    with engine.connect() as conn:
        treasury = conn.scalar(
            text("SELECT id FROM accounts WHERE kind = 'treasury' AND currency = 'USD'")
        )
    assert treasury is not None

    def planted(statements: list[str], params: dict[str, object], key: str) -> None:
        session = SessionLocal()
        try:
            session.begin()
            for statement in statements:
                session.execute(text(statement), params)
            report = invariant_report(session.connection())
            assert report[key] >= 1
            assert violations(session.connection()) >= 1
        finally:
            session.rollback()
            session.close()

    transfer_sql = """
        INSERT INTO transfers (
            id, source_account_id, destination_account_id, amount_minor, currency,
            status, created_at, completed_at
        ) VALUES (
            :id, :source, :destination, :amount, 'USD', 'completed',
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        """
    debit_sql = """
        INSERT INTO ledger_entries (
            id, account_id, transfer_id, funding_id, amount_minor, currency, created_at
        ) VALUES (
            :debit, :debit_account, :id, NULL, :debit_amount, :debit_currency, CURRENT_TIMESTAMP
        )
        """
    credit_sql = """
        INSERT INTO ledger_entries (
            id, account_id, transfer_id, funding_id, amount_minor, currency, created_at
        ) VALUES (
            :credit, :credit_account, :id, NULL, :credit_amount, :credit_currency,
            CURRENT_TIMESTAMP
        )
        """
    planted(
        [transfer_sql],
        {"id": uuid.uuid4(), "source": customer, "destination": other, "amount": 100},
        "transfers_without_entries",
    )
    planted(
        [
            """
            INSERT INTO fundings (
                id, account_id, treasury_account_id, amount_minor, currency, created_at
            ) VALUES (
                :id, :account, :treasury, 100, 'USD', CURRENT_TIMESTAMP
            )
            """
        ],
        {"id": uuid.uuid4(), "account": customer, "treasury": treasury},
        "fundings_without_entries",
    )
    planted(
        [transfer_sql, debit_sql],
        {
            "id": uuid.uuid4(),
            "source": customer,
            "destination": other,
            "amount": 40,
            "debit": uuid.uuid4(),
            "debit_account": customer,
            "debit_amount": -40,
            "debit_currency": "USD",
        },
        "unbalanced_transfers",
    )
    planted(
        [transfer_sql, debit_sql, credit_sql],
        {
            "id": uuid.uuid4(),
            "source": customer,
            "destination": other,
            "amount": 40,
            "debit": uuid.uuid4(),
            "credit": uuid.uuid4(),
            "debit_account": customer,
            "credit_account": other,
            "debit_amount": -40,
            "credit_amount": 25,
            "debit_currency": "USD",
            "credit_currency": "USD",
        },
        "unbalanced_transfers",
    )
    third = _account(client)
    planted(
        [transfer_sql, debit_sql, credit_sql],
        {
            "id": uuid.uuid4(),
            "source": customer,
            "destination": other,
            "amount": 15,
            "debit": uuid.uuid4(),
            "credit": uuid.uuid4(),
            "debit_account": third,
            "credit_account": third,
            "debit_amount": -15,
            "credit_amount": 15,
            "debit_currency": "USD",
            "credit_currency": "USD",
        },
        "transfer_account_mismatches",
    )
    planted(
        [transfer_sql, debit_sql, credit_sql],
        {
            "id": uuid.uuid4(),
            "source": customer,
            "destination": other,
            "amount": 10,
            "debit": uuid.uuid4(),
            "credit": uuid.uuid4(),
            "debit_account": customer,
            "credit_account": other,
            "debit_amount": -10,
            "credit_amount": 10,
            "debit_currency": "EUR",
            "credit_currency": "EUR",
        },
        "currency_mismatches",
    )
    planted(
        [
            """
            INSERT INTO fundings (
                id, account_id, treasury_account_id, amount_minor, currency, created_at
            ) VALUES (
                :funding_id, :account, :treasury, 20, 'USD', CURRENT_TIMESTAMP
            )
            """,
            """
            INSERT INTO ledger_entries (
                id, account_id, transfer_id, funding_id, amount_minor, currency, created_at
            ) VALUES (
                :debit, :account, NULL, :funding_id, 20, 'USD', CURRENT_TIMESTAMP
            )
            """,
            """
            INSERT INTO ledger_entries (
                id, account_id, transfer_id, funding_id, amount_minor, currency, created_at
            ) VALUES (
                :credit, :account, NULL, :funding_id, -20, 'USD', CURRENT_TIMESTAMP
            )
            """,
        ],
        {
            "funding_id": uuid.uuid4(),
            "account": customer,
            "treasury": treasury,
            "debit": uuid.uuid4(),
            "credit": uuid.uuid4(),
        },
        "funding_account_mismatches",
    )
    session = SessionLocal()
    try:
        session.begin()
        session.execute(
            text("UPDATE accounts SET balance_minor = balance_minor + 5 WHERE id = :id"),
            {"id": customer},
        )
        report = invariant_report(session.connection())
        assert report["customer_balance_mismatches"] == 1
        assert report["balance_discrepancy_minor"] == 5
        session.execute(
            text("UPDATE accounts SET balance_minor = balance_minor - 7 WHERE id = :id"),
            {"id": treasury},
        )
        report = invariant_report(session.connection())
        assert report["treasury_balance_mismatches"] == 1
        assert report["customer_balance_mismatches"] == 1
    finally:
        session.rollback()
        session.close()
    with engine.connect() as conn:
        assert violations(conn) == 0


def _deadlocks() -> int:
    with engine.connect() as conn:
        count = conn.scalar(
            text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
        )
    return int(count or 0)
