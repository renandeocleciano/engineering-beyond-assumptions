"""Disposable laboratory database helpers.

Destructive cleanup is refused unless DATABASE_URL points at a recognized
laboratory database. Production and unknown URLs are rejected.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Engine

_ALLOWED_DB_NAMES = frozenset({"experiments"})
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "db", "::1"})

_FINANCIAL_TABLES = (
    "ledger_entries",
    "idempotency_records",
    "fundings",
    "transfers",
    "accounts",
)


def require_laboratory_database_url(url: str | None = None) -> str:
    """Return DATABASE_URL or raise if it is missing or not a laboratory target."""
    raw = url if url is not None else os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL is required for laboratory PostgreSQL tests")
    normalized = raw.replace("postgresql+psycopg", "postgresql", 1)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    db_name = (parsed.path or "").lstrip("/")
    if host not in _ALLOWED_HOSTS:
        raise RuntimeError(f"refusing destructive laboratory cleanup for host {host!r}")
    if db_name not in _ALLOWED_DB_NAMES:
        raise RuntimeError(f"refusing destructive laboratory cleanup for database {db_name!r}")
    return raw


def truncate_financial_tables(engine: Engine) -> None:
    """Reset Experiment 002/003 financial tables. Does not touch counters."""
    require_laboratory_database_url()
    tables = ", ".join(_FINANCIAL_TABLES)
    with engine.begin() as conn:
        # idempotency_records may not exist before migration 003 on a fresh clone.
        exists = conn.scalar(text("SELECT to_regclass('public.idempotency_records')"))
        if exists is None:
            conn.execute(text("TRUNCATE ledger_entries, fundings, transfers, accounts"))
        else:
            conn.execute(text(f"TRUNCATE {tables}"))
