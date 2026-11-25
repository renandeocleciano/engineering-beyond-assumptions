"""Safe account funding and transfers. One database transaction per financial effect."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.ledger.errors import (
    AccountNotFound,
    AccountUnavailable,
    CurrencyMismatch,
    InsufficientFunds,
    TransferNotFound,
)
from app.ledger.models import Account, Funding, LedgerEntry, Transfer


def create_customer_account(session: Session, currency: str) -> Account:
    account = Account(kind="customer", currency=currency, balance_minor=0, status="open", version=0)
    session.add(account)
    session.flush()
    return account


def fund_account(session: Session, account_id: UUID, amount_minor: int) -> Funding:
    """Credit a customer from the per-currency treasury. Laboratory funding only.

    The two accounts are locked in identifier order and refreshed. The balance
    change is a relative SQL update, so a value loaded before the lock is not
    written back.
    """
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    customer = _require_customer(session, account_id)
    treasury_id = _ensure_treasury(session, customer.currency)
    locked = _lock_in_id_order(session, customer.id, treasury_id)
    customer = locked[customer.id]
    treasury = locked[treasury_id]
    _require_open_customer(customer)
    funding = Funding(
        account_id=customer.id,
        treasury_account_id=treasury.id,
        amount_minor=amount_minor,
        currency=customer.currency,
    )
    session.add(funding)
    session.flush()
    _post(session, treasury, -amount_minor, funding_id=funding.id)
    _post(session, customer, amount_minor, funding_id=funding.id)
    return funding


def execute_transfer(
    session: Session,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    currency: str,
    *,
    after_debit: Callable[[], None] | None = None,
) -> Transfer:
    """Transfer inside the caller's database transaction.

    Identifier order on `SELECT … FOR UPDATE` removes the two-account lock
    inversion. `populate_existing` reloads status, currency, and balance for
    any account already present in this session. The debit itself is
    `balance = balance - amount` with a non-negative predicate, so the stored
    balance does not depend on the Python value. The early balance comparison
    avoids inserting a transfer that the predicate would reject. The predicate
    is what prevents the overdraft.

    `after_debit` is a test hook. The HTTP API does not pass it. A raised hook
    aborts the surrounding transaction, so the debit is not committed.
    """
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    if source_account_id == destination_account_id:
        raise ValueError("source and destination must be different")
    locked = _lock_in_id_order(session, source_account_id, destination_account_id)
    source = locked.get(source_account_id)
    destination = locked.get(destination_account_id)
    if source is None or destination is None:
        raise AccountNotFound
    _require_open_customer(source)
    _require_open_customer(destination)
    if source.currency != currency or destination.currency != currency:
        raise CurrencyMismatch
    if source.balance_minor < amount_minor:
        raise InsufficientFunds
    transfer = Transfer(
        source_account_id=source.id,
        destination_account_id=destination.id,
        amount_minor=amount_minor,
        currency=currency,
        status="completed",
    )
    session.add(transfer)
    session.flush()
    _post(session, source, -amount_minor, transfer_id=transfer.id)
    if after_debit is not None:
        after_debit()
    _post(session, destination, amount_minor, transfer_id=transfer.id)
    return transfer


def get_account(session: Session, account_id: UUID) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise AccountNotFound
    return account


def get_transfer(session: Session, transfer_id: UUID) -> Transfer:
    transfer = session.get(Transfer, transfer_id)
    if transfer is None:
        raise TransferNotFound
    return transfer


def list_entries(
    session: Session, account_id: UUID, *, limit: int, offset: int
) -> list[LedgerEntry]:
    get_account(session, account_id)
    return list(
        session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.account_id == account_id)
            .order_by(LedgerEntry.created_at, LedgerEntry.id)
            .limit(limit)
            .offset(offset)
        )
    )


def _require_customer(session: Session, account_id: UUID) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise AccountNotFound
    _require_open_customer(account)
    return account


def _require_open_customer(account: Account) -> None:
    if account.kind != "customer" or account.status != "open":
        raise AccountUnavailable


def _ensure_treasury(session: Session, currency: str) -> UUID:
    existing = session.scalar(
        select(Account.id).where(Account.kind == "treasury", Account.currency == currency)
    )
    if existing is not None:
        return existing
    treasury = Account(
        kind="treasury", currency=currency, balance_minor=0, status="open", version=0
    )
    try:
        with session.begin_nested():
            session.add(treasury)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(Account.id).where(Account.kind == "treasury", Account.currency == currency)
        )
        if existing is None:
            raise
        return existing
    return treasury.id


def _lock_in_id_order(session: Session, first: UUID, second: UUID) -> dict[UUID, Account]:
    """Lock in sorted identifier order and replace any identity-map state."""
    locked: dict[UUID, Account] = {}
    for account_id in sorted((first, second)):
        account = session.execute(
            select(Account).where(Account.id == account_id).with_for_update(),
            execution_options={"populate_existing": True},
        ).scalar_one_or_none()
        if account is not None:
            locked[account.id] = account
    return locked


def _apply_delta(session: Session, account: Account, delta: int) -> None:
    """Add `delta` to the database balance. A rejected customer debit writes nothing."""
    if delta == 0:
        raise ValueError("delta must be nonzero")
    statement = update(Account).where(Account.id == account.id)
    if delta < 0:
        statement = statement.where(
            or_(Account.kind == "treasury", Account.balance_minor + delta >= 0)
        )
    statement = statement.values(
        balance_minor=Account.balance_minor + delta,
        version=Account.version + 1,
        updated_at=datetime.now(UTC),
    ).returning(Account.balance_minor, Account.version, Account.updated_at)
    row = session.execute(
        statement, execution_options={"synchronize_session": False}
    ).one_or_none()
    if row is None:
        if delta < 0 and account.kind != "treasury":
            raise InsufficientFunds
        raise AccountNotFound
    set_committed_value(account, "balance_minor", int(row.balance_minor))
    set_committed_value(account, "version", int(row.version))
    set_committed_value(account, "updated_at", row.updated_at)


def _post(
    session: Session,
    account: Account,
    amount_minor: int,
    *,
    transfer_id: UUID | None = None,
    funding_id: UUID | None = None,
) -> None:
    _apply_delta(session, account, amount_minor)
    session.add(
        LedgerEntry(
            account_id=account.id,
            transfer_id=transfer_id,
            funding_id=funding_id,
            amount_minor=amount_minor,
            currency=account.currency,
        )
    )
    session.flush()
