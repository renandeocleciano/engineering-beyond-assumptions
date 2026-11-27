"""Shared pytest fixtures. Laboratory financial tables are reset before each postgres test."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import engine
from app.laboratory import require_laboratory_database_url, truncate_financial_tables
from app.ledger.faults import clear_faults
from app.main import app


@pytest.fixture
def laboratory_database() -> Iterator[None]:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("set DATABASE_URL to a migrated PostgreSQL database")
    require_laboratory_database_url()
    clear_faults()
    truncate_financial_tables(engine)
    yield
    clear_faults()
    truncate_financial_tables(engine)


@pytest.fixture
def client(laboratory_database: None) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
