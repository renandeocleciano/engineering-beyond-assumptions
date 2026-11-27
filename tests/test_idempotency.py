"""Experiment 003: idempotency under concurrency, lost responses, and rollbacks."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.laboratory import require_laboratory_database_url
from app.ledger.errors import IdempotencyKeyConflict, InsufficientFunds
from app.ledger.faults import clear_faults, set_fault
from app.ledger.fingerprint import transfer_fingerprint, validate_idempotency_key
from app.ledger.idempotency import execute_idempotent_transfer
from app.ledger.models import Account
from app.ledger.reconcile import violations
from app.ledger.service import fund_account
from app.main import app

pytestmark = pytest.mark.postgres


def _account(client: TestClient, currency: str = "USD") -> str:
    response = client.post("/accounts", json={"currency": currency})
    assert response.status_code == 201
    account_id = response.json()["id"]
    assert isinstance(account_id, str)
    return account_id


def _fund(client: TestClient, account_id: str, amount: int) -> None:
    response = client.post(f"/accounts/{account_id}/fund", json={"amount_minor": amount})
    assert response.status_code == 200


def _payload(source: str, destination: str, amount: int = 500) -> dict[str, object]:
    return {
        "source_account_id": source,
        "destination_account_id": destination,
        "amount_minor": amount,
        "currency": "USD",
    }


def _assert_clean() -> None:
    with engine.connect() as conn:
        assert violations(conn) == 0


def test_lab_db_refuses_non_laboratory_url() -> None:
    with pytest.raises(RuntimeError, match="refusing"):
        require_laboratory_database_url(
            "postgresql+psycopg://user:pass@localhost:5432/production"
        )


def test_fingerprint_is_stable_and_key_validation() -> None:
    source = uuid.uuid4()
    destination = uuid.uuid4()
    first = transfer_fingerprint(
        source_account_id=source,
        destination_account_id=destination,
        amount_minor=100,
        currency="usd",
    )
    second = transfer_fingerprint(
        source_account_id=source,
        destination_account_id=destination,
        amount_minor=100,
        currency="USD",
    )
    assert first == second
    assert len(first) == 64
    assert validate_idempotency_key("transfer-key-01") == "transfer-key-01"
    with pytest.raises(ValueError):
        validate_idempotency_key("short")


def test_sequential_duplicate_http(client: TestClient) -> None:
    source = _account(client)
    destination = _account(client)
    _fund(client, source, 2000)
    headers = {"Idempotency-Key": "sequential-duplicate-01"}
    body = _payload(source, destination, 500)
    first = client.post("/transfers", headers=headers, json=body)
    second = client.post("/transfers", headers=headers, json=body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["idempotent_replay"] is False
    assert second.json()["idempotent_replay"] is True
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 1500
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 500
    with engine.connect() as conn:
        transfers = conn.scalar(text("SELECT count(*) FROM transfers"))
        entries = conn.scalar(
            text("SELECT count(*) FROM ledger_entries WHERE transfer_id IS NOT NULL")
        )
        records = conn.scalar(text("SELECT count(*) FROM idempotency_records"))
        assert violations(conn) == 0
    assert transfers == 1
    assert entries == 2
    assert records == 1


def test_concurrent_duplicate_same_key(client: TestClient) -> None:
    source = uuid.UUID(_account(client))
    destination = uuid.UUID(_account(client))
    _fund(client, str(source), 2000)
    barrier = Barrier(8)
    key = "concurrent-duplicate-01"

    def attempt() -> str:
        barrier.wait(timeout=10)
        with SessionLocal() as session, session.begin():
            outcome = execute_idempotent_transfer(
                session,
                idempotency_key=key,
                source_account_id=source,
                destination_account_id=destination,
                amount_minor=500,
                currency="USD",
            )
            return str(outcome.transfer.id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        transfer_ids = list(executor.map(lambda _: attempt(), range(8)))
    assert len(set(transfer_ids)) == 1
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 1500
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 500
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 1
        assert violations(conn) == 0


def test_payload_conflict_http(client: TestClient) -> None:
    source = _account(client)
    first_dest = _account(client)
    second_dest = _account(client)
    _fund(client, source, 2000)
    headers = {"Idempotency-Key": "payload-conflict-01"}
    created = client.post(
        "/transfers", headers=headers, json=_payload(source, first_dest, 500)
    )
    conflict = client.post(
        "/transfers", headers=headers, json=_payload(source, second_dest, 500)
    )
    amount_conflict = client.post(
        "/transfers", headers=headers, json=_payload(source, first_dest, 100)
    )
    assert created.status_code == 201
    assert conflict.status_code == 409
    assert amount_conflict.status_code == 409
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 1500
    assert client.get(f"/accounts/{first_dest}").json()["balance_minor"] == 500
    assert client.get(f"/accounts/{second_dest}").json()["balance_minor"] == 0
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 1
        assert violations(conn) == 0


def test_lost_response_after_commit_http(client: TestClient) -> None:
    source = _account(client)
    destination = _account(client)
    _fund(client, source, 2000)
    headers = {"Idempotency-Key": "lost-response-01"}
    body = _payload(source, destination, 500)
    set_fault("after_commit_before_response", lambda: (_ for _ in ()).throw(RuntimeError("lost")))
    failing = TestClient(app, raise_server_exceptions=False)
    first = failing.post("/transfers", headers=headers, json=body)
    clear_faults()
    assert first.status_code == 500
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 1
        assert violations(conn) == 0
    retry = client.post("/transfers", headers=headers, json=body)
    assert retry.status_code == 200
    assert retry.json()["idempotent_replay"] is True
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 1500
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 500
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 1
        assert violations(conn) == 0


def test_rollback_before_commit_allows_retry(client: TestClient) -> None:
    source = uuid.UUID(_account(client))
    destination = uuid.UUID(_account(client))
    _fund(client, str(source), 2000)
    key = "rollback-before-commit-01"
    set_fault("before_commit", lambda: (_ for _ in ()).throw(RuntimeError("abort")))
    with pytest.raises(RuntimeError, match="abort"):
        with SessionLocal() as session, session.begin():
            execute_idempotent_transfer(
                session,
                idempotency_key=key,
                source_account_id=source,
                destination_account_id=destination,
                amount_minor=500,
                currency="USD",
            )
    clear_faults()
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 0
        assert conn.scalar(text("SELECT count(*) FROM idempotency_records")) == 0
        assert violations(conn) == 0
    with SessionLocal() as session, session.begin():
        outcome = execute_idempotent_transfer(
            session,
            idempotency_key=key,
            source_account_id=source,
            destination_account_id=destination,
            amount_minor=500,
            currency="USD",
        )
        assert outcome.replay is False
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 1500
    _assert_clean()


def test_rollback_after_debit_clears_reservation(client: TestClient) -> None:
    source = uuid.UUID(_account(client))
    destination = uuid.UUID(_account(client))
    _fund(client, str(source), 2000)
    key = "rollback-after-debit-01"
    set_fault(
        "after_debit_before_credit",
        lambda: (_ for _ in ()).throw(RuntimeError("debit-only")),
    )
    with pytest.raises(RuntimeError, match="debit-only"):
        with SessionLocal() as session, session.begin():
            execute_idempotent_transfer(
                session,
                idempotency_key=key,
                source_account_id=source,
                destination_account_id=destination,
                amount_minor=500,
                currency="USD",
            )
    clear_faults()
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 0
        assert conn.scalar(text("SELECT count(*) FROM ledger_entries")) == 2  # funding only
        assert conn.scalar(text("SELECT count(*) FROM idempotency_records")) == 0
    with SessionLocal() as session, session.begin():
        execute_idempotent_transfer(
            session,
            idempotency_key=key,
            source_account_id=source,
            destination_account_id=destination,
            amount_minor=500,
            currency="USD",
        )
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 1500
    _assert_clean()


def test_concurrent_different_keys_respect_balance(client: TestClient) -> None:
    source = uuid.UUID(_account(client))
    first = uuid.UUID(_account(client))
    second = uuid.UUID(_account(client))
    _fund(client, str(source), 1000)
    barrier = Barrier(2)

    def attempt(destination: uuid.UUID, key: str) -> str:
        barrier.wait(timeout=10)
        try:
            with SessionLocal() as session, session.begin():
                execute_idempotent_transfer(
                    session,
                    idempotency_key=key,
                    source_account_id=source,
                    destination_account_id=destination,
                    amount_minor=800,
                    currency="USD",
                )
            return "ok"
        except InsufficientFunds:
            return "poor"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: attempt(*item),
                ((first, "diff-key-a-01"), (second, "diff-key-b-01")),
            )
        )
    assert sorted(results) == ["ok", "poor"]
    balances = sorted(
        client.get(f"/accounts/{account}").json()["balance_minor"]
        for account in (source, first, second)
    )
    assert balances == [0, 200, 800]
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 1
        assert violations(conn) == 0


def test_idempotent_transfer_with_previously_loaded_accounts(client: TestClient) -> None:
    source = uuid.UUID(_account(client))
    destination = uuid.UUID(_account(client))
    _fund(client, str(source), 500)
    with SessionLocal() as session, session.begin():
        loaded = session.get(Account, source)
        assert loaded is not None
        assert loaded.balance_minor == 500
        fund_account(session, source, 50)
        outcome = execute_idempotent_transfer(
            session,
            idempotency_key="stale-session-key-01",
            source_account_id=source,
            destination_account_id=destination,
            amount_minor=30,
            currency="USD",
        )
        assert outcome.replay is False
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 520
    assert client.get(f"/accounts/{destination}").json()["balance_minor"] == 30
    _assert_clean()


def test_business_rejection_does_not_retain_reservation(client: TestClient) -> None:
    source = _account(client)
    destination = _account(client)
    _fund(client, source, 100)
    headers = {"Idempotency-Key": "business-reject-01"}
    poor = client.post("/transfers", headers=headers, json=_payload(source, destination, 500))
    assert poor.status_code == 409
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 0
        assert conn.scalar(text("SELECT count(*) FROM idempotency_records")) == 0
    _fund(client, source, 500)
    retry = client.post("/transfers", headers=headers, json=_payload(source, destination, 500))
    assert retry.status_code == 201
    assert client.get(f"/accounts/{source}").json()["balance_minor"] == 100
    _assert_clean()


def test_service_payload_conflict_raises(client: TestClient) -> None:
    source = uuid.UUID(_account(client))
    first = uuid.UUID(_account(client))
    second = uuid.UUID(_account(client))
    _fund(client, str(source), 2000)
    with SessionLocal() as session, session.begin():
        execute_idempotent_transfer(
            session,
            idempotency_key="service-conflict-01",
            source_account_id=source,
            destination_account_id=first,
            amount_minor=100,
            currency="USD",
        )
    with pytest.raises(IdempotencyKeyConflict):
        with SessionLocal() as session, session.begin():
            execute_idempotent_transfer(
                session,
                idempotency_key="service-conflict-01",
                source_account_id=source,
                destination_account_id=second,
                amount_minor=100,
                currency="USD",
            )
    _assert_clean()


def test_suite_isolation_leaves_no_residual_transfers(client: TestClient) -> None:
    """Running after other tests must start empty; fixture truncate is the proof."""
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 0
        assert conn.scalar(text("SELECT count(*) FROM idempotency_records")) == 0
    source = _account(client)
    destination = _account(client)
    _fund(client, source, 100)
    response = client.post(
        "/transfers",
        headers={"Idempotency-Key": "isolation-marker-01"},
        json=_payload(source, destination, 10),
    )
    assert response.status_code == 201
