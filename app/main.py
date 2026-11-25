from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.ledger.api import router as ledger_router
from app.models import Counter

app = FastAPI(title="Engineering Beyond Assumptions", version="0.1.0")
app.include_router(ledger_router)
SessionDep = Annotated[Session, Depends(get_session)]


class CounterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class CounterResponse(BaseModel):
    name: str
    value: int


@app.get("/health")
def health() -> dict[str, str]:
    """Process liveness. This does not check database connectivity."""
    return {"status": "ok"}


@app.post("/counters", response_model=CounterResponse, status_code=201)
def create_counter(payload: CounterCreate, session: SessionDep) -> Counter:
    counter = Counter(name=payload.name, value=0)
    session.add(counter)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Counter already exists") from exc
    session.refresh(counter)
    return counter


@app.get("/counters/{name}", response_model=CounterResponse)
def get_counter(name: str, session: SessionDep) -> Counter:
    counter = session.scalar(select(Counter).where(Counter.name == name))
    if counter is None:
        raise HTTPException(status_code=404, detail="Counter not found")
    return counter


@app.post("/counters/{name}/increment", response_model=CounterResponse)
def increment_counter(name: str, session: SessionDep) -> CounterResponse:
    row = session.execute(
        update(Counter)
        .where(Counter.name == name)
        .values(value=Counter.value + 1)
        .returning(Counter.name, Counter.value)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Counter not found")
    session.commit()
    return CounterResponse(name=row.name, value=row.value)
