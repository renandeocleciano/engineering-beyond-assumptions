"""HTTP contracts for laboratory accounts and transfers."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.db import get_session
from app.ledger.errors import (
    AccountNotFound,
    AccountUnavailable,
    CurrencyMismatch,
    InsufficientFunds,
    TransferNotFound,
)
from app.ledger.models import Account, Funding, LedgerEntry, Transfer
from app.ledger.service import (
    create_customer_account,
    execute_transfer,
    fund_account,
    get_account,
    get_transfer,
    list_entries,
)

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]
_CURRENCY = Field(pattern=r"^[A-Z]{3}$")


class AccountCreate(BaseModel):
    currency: str = _CURRENCY


class AccountResponse(BaseModel):
    id: UUID
    kind: str
    currency: str
    balance_minor: int
    status: str
    created_at: datetime
    updated_at: datetime


class FundRequest(BaseModel):
    amount_minor: int = Field(gt=0)


class FundingResponse(BaseModel):
    id: UUID
    account_id: UUID
    amount_minor: int
    currency: str
    balance_minor: int


class TransferCreate(BaseModel):
    source_account_id: UUID
    destination_account_id: UUID
    amount_minor: int = Field(gt=0)
    currency: str = _CURRENCY

    @model_validator(mode="after")
    def accounts_differ(self) -> "TransferCreate":
        if self.source_account_id == self.destination_account_id:
            raise ValueError("Source and destination accounts must be different")
        return self


class TransferResponse(BaseModel):
    id: UUID
    source_account_id: UUID
    destination_account_id: UUID
    amount_minor: int
    currency: str
    status: str
    created_at: datetime
    completed_at: datetime


class EntryResponse(BaseModel):
    id: UUID
    account_id: UUID
    transfer_id: UUID | None
    funding_id: UUID | None
    amount_minor: int
    currency: str
    created_at: datetime


class EntryPage(BaseModel):
    items: list[EntryResponse]
    limit: int
    offset: int


def _account_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        kind=account.kind,
        currency=account.currency,
        balance_minor=account.balance_minor,
        status=account.status,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.post("/accounts", response_model=AccountResponse, status_code=201)
def post_account(payload: AccountCreate, session: SessionDep) -> AccountResponse:
    account = create_customer_account(session, payload.currency)
    session.commit()
    session.refresh(account)
    return _account_response(account)


@router.get("/accounts/{account_id}", response_model=AccountResponse)
def read_account(account_id: UUID, session: SessionDep) -> AccountResponse:
    try:
        account = get_account(session, account_id)
    except AccountNotFound as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
    return _account_response(account)


@router.post("/accounts/{account_id}/fund", response_model=FundingResponse)
def post_fund(account_id: UUID, payload: FundRequest, session: SessionDep) -> FundingResponse:
    """Add synthetic funds. Laboratory setup only; this is not a production deposit."""
    try:
        funding = fund_account(session, account_id, payload.amount_minor)
        account = get_account(session, account_id)
        session.commit()
        session.refresh(funding)
    except AccountNotFound as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Account not found") from exc
    except AccountUnavailable as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Account cannot be funded") from exc
    return _funding_response(funding, account.balance_minor)


@router.post("/transfers", response_model=TransferResponse, status_code=201)
def post_transfer(payload: TransferCreate, session: SessionDep) -> TransferResponse:
    try:
        transfer = execute_transfer(
            session,
            payload.source_account_id,
            payload.destination_account_id,
            payload.amount_minor,
            payload.currency,
        )
        session.commit()
        session.refresh(transfer)
    except AccountNotFound as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Account not found") from exc
    except AccountUnavailable as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Account cannot transfer") from exc
    except CurrencyMismatch as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Account currencies must match") from exc
    except InsufficientFunds as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Insufficient funds") from exc
    return _transfer_response(transfer)


@router.get("/transfers/{transfer_id}", response_model=TransferResponse)
def read_transfer(transfer_id: UUID, session: SessionDep) -> TransferResponse:
    try:
        transfer = get_transfer(session, transfer_id)
    except TransferNotFound as exc:
        raise HTTPException(status_code=404, detail="Transfer not found") from exc
    return _transfer_response(transfer)


@router.get("/accounts/{account_id}/entries", response_model=EntryPage)
def read_entries(
    account_id: UUID,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EntryPage:
    try:
        entries = list_entries(session, account_id, limit=limit, offset=offset)
    except AccountNotFound as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
    return EntryPage(
        items=[_entry_response(entry) for entry in entries], limit=limit, offset=offset
    )


def _funding_response(funding: Funding, balance_minor: int) -> FundingResponse:
    return FundingResponse(
        id=funding.id,
        account_id=funding.account_id,
        amount_minor=funding.amount_minor,
        currency=funding.currency,
        balance_minor=balance_minor,
    )


def _transfer_response(transfer: Transfer) -> TransferResponse:
    return TransferResponse(
        id=transfer.id,
        source_account_id=transfer.source_account_id,
        destination_account_id=transfer.destination_account_id,
        amount_minor=transfer.amount_minor,
        currency=transfer.currency,
        status=transfer.status,
        created_at=transfer.created_at,
        completed_at=transfer.completed_at,
    )


def _entry_response(entry: LedgerEntry) -> EntryResponse:
    return EntryResponse(
        id=entry.id,
        account_id=entry.account_id,
        transfer_id=entry.transfer_id,
        funding_id=entry.funding_id,
        amount_minor=entry.amount_minor,
        currency=entry.currency,
        created_at=entry.created_at,
    )
