"""Intentionally unsafe retry strategies for Experiment 003.

These paths are not exposed on the public API. They exist to reproduce
duplicate financial effects when a client retries without a coordinated
idempotency record, and to show that a naive check-then-insert race is
insufficient without a uniqueness constraint and transactional coordination.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ledger.service import execute_transfer


@dataclass(frozen=True)
class UnsafeOutcome:
    transfer_id: UUID
    created: bool


def retry_without_idempotency(
    session: Session,
    *,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    currency: str,
) -> UnsafeOutcome:
    """Always execute a new transfer. A lost-response retry duplicates money movement."""
    transfer = execute_transfer(
        session,
        source_account_id,
        destination_account_id,
        amount_minor,
        currency,
    )
    return UnsafeOutcome(transfer_id=transfer.id, created=True)


def naive_check_then_insert(
    session: Session,
    *,
    scope: str,
    key: str,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    currency: str,
    after_missed_lookup: Callable[[], None] | None = None,
) -> UnsafeOutcome:
    """SELECT then INSERT without a uniqueness wait path.

    Uses an experimental table without a unique (scope, key) constraint.
    Concurrent callers can both observe absence and both create transfers.
    The production `idempotency_records` unique index is never weakened.

    `after_missed_lookup` is a test hook (for example a barrier) that runs after a
    miss is observed and before the transfer, so the race window is deterministic.

    Call `ensure_unsafe_table` once before concurrent workers start. This function
    does not create the table, so workers do not contend on DDL locks.
    """
    existing = session.execute(
        text(
            "SELECT transfer_id FROM unsafe_idempotency_attempts "
            "WHERE scope = :scope AND attempt_key = :key"
        ),
        {"scope": scope, "key": key},
    ).first()
    if existing is not None:
        return UnsafeOutcome(transfer_id=UUID(str(existing.transfer_id)), created=False)
    if after_missed_lookup is not None:
        after_missed_lookup()
    transfer = execute_transfer(
        session,
        source_account_id,
        destination_account_id,
        amount_minor,
        currency,
    )
    session.execute(
        text(
            "INSERT INTO unsafe_idempotency_attempts "
            "(scope, attempt_key, transfer_id) VALUES (:scope, :key, :transfer_id)"
        ),
        {"scope": scope, "key": key, "transfer_id": transfer.id},
    )
    session.flush()
    return UnsafeOutcome(transfer_id=transfer.id, created=True)


def ensure_unsafe_table(session: Session) -> None:
    """Create the experimental table once. Call before concurrent workers start."""
    session.execute(text("SELECT pg_advisory_xact_lock(903001)"))
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS unsafe_idempotency_attempts (
                id bigserial PRIMARY KEY,
                scope text NOT NULL,
                attempt_key text NOT NULL,
                transfer_id uuid NOT NULL
            )
            """
        )
    )
    session.flush()

