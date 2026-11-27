"""Unsafe Experiment 003 strategies must demonstrate duplicate effects without touching the API."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.ledger.models import Account
from app.ledger.reconcile import violations
from app.ledger.service import create_customer_account, fund_account
from experiments.experiment_003.unsafe import (
    ensure_unsafe_table,
    naive_check_then_insert,
    retry_without_idempotency,
)

pytestmark = pytest.mark.postgres


def test_retry_without_idempotency_duplicates_transfer(laboratory_database: None) -> None:
    with SessionLocal() as session, session.begin():
        source = create_customer_account(session, "USD")
        destination = create_customer_account(session, "USD")
        fund_account(session, source.id, 2000)
        source_id, destination_id = source.id, destination.id
    with SessionLocal() as session, session.begin():
        retry_without_idempotency(
            session,
            source_account_id=source_id,
            destination_account_id=destination_id,
            amount_minor=500,
            currency="USD",
        )
    with SessionLocal() as session, session.begin():
        retry_without_idempotency(
            session,
            source_account_id=source_id,
            destination_account_id=destination_id,
            amount_minor=500,
            currency="USD",
        )
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM transfers")) == 2
        assert violations(conn) == 0
    with SessionLocal() as session:
        source_balance = session.get(Account, source_id)
        assert source_balance is not None
        assert source_balance.balance_minor == 1000


def test_naive_check_then_insert_can_duplicate_under_concurrency(
    laboratory_database: None,
) -> None:
    with SessionLocal() as session, session.begin():
        source = create_customer_account(session, "USD")
        destination = create_customer_account(session, "USD")
        fund_account(session, source.id, 5000)
        ensure_unsafe_table(session)
        source_id, destination_id = source.id, destination.id
    start = Barrier(8)
    after_lookup = Barrier(8)
    key = f"naive-{uuid.uuid4()}"

    def attempt() -> str:
        start.wait(timeout=10)
        with SessionLocal() as session, session.begin():
            outcome = naive_check_then_insert(
                session,
                scope="lab.unsafe",
                key=key,
                source_account_id=source_id,
                destination_account_id=destination_id,
                amount_minor=100,
                currency="USD",
                after_missed_lookup=lambda: None if after_lookup.wait(timeout=10) else None,
            )
            return str(outcome.transfer_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(lambda _: attempt(), range(8)))
    with engine.connect() as conn:
        transfer_count = int(conn.scalar(text("SELECT count(*) FROM transfers")) or 0)
        assert violations(conn) == 0
    # Deterministic race: every worker observed a miss before any insert committed.
    assert transfer_count > 1
    assert len(set(ids)) == transfer_count
