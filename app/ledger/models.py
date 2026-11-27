"""Ledger tables. Customer balances are projections of signed ledger entries."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("kind IN ('customer', 'treasury')", name="ck_accounts_kind"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_accounts_status"),
        CheckConstraint("balance_minor >= 0 OR kind = 'treasury'", name="ck_accounts_balance"),
        CheckConstraint("char_length(currency) = 3", name="ck_accounts_currency"),
        Index(
            "uq_accounts_treasury_currency",
            "currency",
            unique=True,
            postgresql_where=text("kind = 'treasury'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Funding(Base):
    """Laboratory-only synthetic funding. Not a production deposit."""

    __tablename__ = "fundings"
    __table_args__ = (CheckConstraint("amount_minor > 0", name="ck_fundings_amount"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    treasury_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_transfers_amount"),
        CheckConstraint(
            "source_account_id <> destination_account_id", name="ck_transfers_distinct"
        ),
        CheckConstraint("status IN ('completed')", name="ck_transfers_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    destination_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class IdempotencyRecord(Base):
    """Durable identity for a successful laboratory transfer.

    A reserved row exists only inside an open transaction. Completed rows are
    committed with the transfer they protect. Scope is laboratory-wide; the API
    has no tenant or authentication model yet.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("status IN ('reserved', 'completed')", name="ck_idempotency_status"),
        CheckConstraint(
            "(status = 'reserved' AND transfer_id IS NULL AND completed_at IS NULL) "
            "OR (status = 'completed' AND transfer_id IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_idempotency_status_shape",
        ),
        Index(
            "uq_idempotency_scope_key",
            "scope",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_idempotency_records_transfer_id",
            "transfer_id",
            unique=True,
            postgresql_where=text("transfer_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    transfer_id: Mapped[UUID | None] = mapped_column(ForeignKey("transfers.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LedgerEntry(Base):
    """Immutable signed movement. Positive credits a customer; negative debits a customer."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="ck_ledger_nonzero"),
        CheckConstraint(
            "(transfer_id IS NOT NULL AND funding_id IS NULL) "
            "OR (transfer_id IS NULL AND funding_id IS NOT NULL)",
            name="ck_ledger_one_cause",
        ),
        Index("ix_ledger_entries_account_created", "account_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    transfer_id: Mapped[UUID | None] = mapped_column(ForeignKey("transfers.id"), nullable=True)
    funding_id: Mapped[UUID | None] = mapped_column(ForeignKey("fundings.id"), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
