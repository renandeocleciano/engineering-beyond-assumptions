"""Transactional idempotency for laboratory transfers.

Lock order inside one PostgreSQL transaction:
1. Reserve the (scope, key) row via INSERT (unique index serialization).
2. Lock accounts in sorted UUID order (existing transfer path).
3. Insert ledger rows and complete the idempotency record.

A reserved row is never committed without its transfer. Business rejections
roll the reservation back so a later retry can reevaluate balances.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ledger.errors import IdempotencyKeyConflict
from app.ledger.faults import maybe_fault
from app.ledger.fingerprint import TRANSFER_SCOPE, transfer_fingerprint, validate_idempotency_key
from app.ledger.models import IdempotencyRecord, Transfer
from app.ledger.service import execute_transfer, get_transfer


@dataclass(frozen=True)
class IdempotentTransferResult:
    transfer: Transfer
    replay: bool


def execute_idempotent_transfer(
    session: Session,
    *,
    idempotency_key: str,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    currency: str,
) -> IdempotentTransferResult:
    key = validate_idempotency_key(idempotency_key)
    fingerprint = transfer_fingerprint(
        source_account_id=source_account_id,
        destination_account_id=destination_account_id,
        amount_minor=amount_minor,
        currency=currency,
    )
    maybe_fault("before_idempotency_reservation")
    reserved = _try_reserve(session, key=key, fingerprint=fingerprint)
    if reserved is None:
        return _replay_or_conflict(session, key=key, fingerprint=fingerprint)
    maybe_fault("after_reservation_before_transfer")

    def after_debit() -> None:
        maybe_fault("after_debit_before_credit")

    transfer = execute_transfer(
        session,
        source_account_id,
        destination_account_id,
        amount_minor,
        currency,
        after_debit=after_debit,
    )
    reserved.transfer_id = transfer.id
    reserved.status = "completed"
    reserved.completed_at = datetime.now(UTC)
    session.flush()
    maybe_fault("before_commit")
    return IdempotentTransferResult(transfer=transfer, replay=False)


def _try_reserve(session: Session, *, key: str, fingerprint: str) -> IdempotencyRecord | None:
    record = IdempotencyRecord(
        scope=TRANSFER_SCOPE,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        status="reserved",
    )
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        return None
    return record


def _replay_or_conflict(
    session: Session, *, key: str, fingerprint: str
) -> IdempotentTransferResult:
    existing = session.execute(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.scope == TRANSFER_SCOPE,
            IdempotencyRecord.idempotency_key == key,
        )
        .with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one()
    if existing.request_fingerprint != fingerprint:
        raise IdempotencyKeyConflict
    if existing.status != "completed" or existing.transfer_id is None:
        raise RuntimeError("idempotency reservation committed without a transfer")
    transfer = get_transfer(session, existing.transfer_id)
    return IdempotentTransferResult(transfer=transfer, replay=True)
