"""Independent checks of balance projections against ledger movements.

Database constraints reject a negative customer balance, a non-positive
movement amount, and a ledger amount of zero. They do not require a movement
to have two balancing entries, the right accounts, or a matching currency.
This report detects those breaks. It does not repair them.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

_COUNT_KEYS = (
    "customer_balance_mismatches",
    "treasury_balance_mismatches",
    "transfers_without_entries",
    "fundings_without_entries",
    "unbalanced_transfers",
    "unbalanced_fundings",
    "transfer_account_mismatches",
    "funding_account_mismatches",
    "currency_mismatches",
)


def invariant_report(connection: Connection) -> dict[str, int]:
    customer_mismatches = _count(
        connection,
        """
        SELECT count(*) FROM (
            SELECT a.id
            FROM accounts a
            LEFT JOIN ledger_entries e ON e.account_id = a.id
            WHERE a.kind = 'customer'
            GROUP BY a.id, a.balance_minor
            HAVING a.balance_minor <> COALESCE(SUM(e.amount_minor), 0)
        ) mismatched
        """,
    )
    treasury_mismatches = _count(
        connection,
        """
        SELECT count(*) FROM (
            SELECT a.id
            FROM accounts a
            LEFT JOIN ledger_entries e ON e.account_id = a.id
            WHERE a.kind = 'treasury'
            GROUP BY a.id, a.balance_minor
            HAVING a.balance_minor <> COALESCE(SUM(e.amount_minor), 0)
        ) mismatched
        """,
    )
    discrepancy = _count(
        connection,
        """
        SELECT COALESCE(SUM(ABS(a.balance_minor - COALESCE(sums.total, 0))), 0)
        FROM accounts a
        LEFT JOIN (
            SELECT account_id, SUM(amount_minor) AS total
            FROM ledger_entries
            GROUP BY account_id
        ) sums ON sums.account_id = a.id
        """,
    )
    report = {
        "customer_balance_mismatches": customer_mismatches,
        "treasury_balance_mismatches": treasury_mismatches,
        "balance_mismatches": customer_mismatches + treasury_mismatches,
        "balance_discrepancy_minor": discrepancy,
        "transfers_without_entries": _count(
            connection,
            """
            SELECT count(*) FROM transfers t
            WHERE NOT EXISTS (
                SELECT 1 FROM ledger_entries e WHERE e.transfer_id = t.id
            )
            """,
        ),
        "fundings_without_entries": _count(
            connection,
            """
            SELECT count(*) FROM fundings f
            WHERE NOT EXISTS (
                SELECT 1 FROM ledger_entries e WHERE e.funding_id = f.id
            )
            """,
        ),
        "unbalanced_transfers": _count(
            connection,
            """
            SELECT count(*) FROM (
                SELECT transfer_id
                FROM ledger_entries
                WHERE transfer_id IS NOT NULL
                GROUP BY transfer_id
                HAVING SUM(amount_minor) <> 0 OR count(*) <> 2
            ) bad_transfers
            """,
        ),
        "unbalanced_fundings": _count(
            connection,
            """
            SELECT count(*) FROM (
                SELECT funding_id
                FROM ledger_entries
                WHERE funding_id IS NOT NULL
                GROUP BY funding_id
                HAVING SUM(amount_minor) <> 0 OR count(*) <> 2
            ) bad_fundings
            """,
        ),
        "transfer_account_mismatches": _count(connection, _TRANSFER_ACCOUNTS),
        "funding_account_mismatches": _count(connection, _FUNDING_ACCOUNTS),
        "currency_mismatches": _count(connection, _CURRENCY),
        "ledger_sum": _count(
            connection, "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries"
        ),
    }
    return report


def violation_count(report: dict[str, int]) -> int:
    """Sum of detectors. One movement can increment more than one detector."""
    total = sum(report[key] for key in _COUNT_KEYS)
    if report["ledger_sum"] != 0:
        total += 1
    return total


def violations(connection: Connection) -> int:
    return violation_count(invariant_report(connection))


def _count(connection: Connection, sql: str) -> int:
    value = connection.scalar(text(sql))
    return int(value or 0)


_TRANSFER_ACCOUNTS = """
SELECT count(*) FROM transfers t
WHERE EXISTS (SELECT 1 FROM ledger_entries e WHERE e.transfer_id = t.id)
AND (
    EXISTS (
        SELECT 1 FROM ledger_entries e
        WHERE e.transfer_id = t.id
          AND e.account_id <> t.source_account_id
          AND e.account_id <> t.destination_account_id
    )
    OR NOT EXISTS (
        SELECT 1 FROM ledger_entries e
        WHERE e.transfer_id = t.id
          AND e.account_id = t.source_account_id
          AND e.amount_minor = -t.amount_minor
    )
    OR NOT EXISTS (
        SELECT 1 FROM ledger_entries e
        WHERE e.transfer_id = t.id
          AND e.account_id = t.destination_account_id
          AND e.amount_minor = t.amount_minor
    )
)
"""

_FUNDING_ACCOUNTS = """
SELECT count(*) FROM fundings f
WHERE EXISTS (SELECT 1 FROM ledger_entries e WHERE e.funding_id = f.id)
AND (
    EXISTS (
        SELECT 1 FROM ledger_entries e
        WHERE e.funding_id = f.id
          AND e.account_id <> f.account_id
          AND e.account_id <> f.treasury_account_id
    )
    OR NOT EXISTS (
        SELECT 1 FROM ledger_entries e
        WHERE e.funding_id = f.id
          AND e.account_id = f.account_id
          AND e.amount_minor = f.amount_minor
    )
    OR NOT EXISTS (
        SELECT 1 FROM ledger_entries e
        WHERE e.funding_id = f.id
          AND e.account_id = f.treasury_account_id
          AND e.amount_minor = -f.amount_minor
    )
)
"""

_CURRENCY = """
SELECT
    (
        SELECT count(*) FROM ledger_entries e
        JOIN accounts a ON a.id = e.account_id
        LEFT JOIN transfers t ON t.id = e.transfer_id
        LEFT JOIN fundings f ON f.id = e.funding_id
        WHERE e.currency <> a.currency
           OR (t.id IS NOT NULL AND e.currency <> t.currency)
           OR (f.id IS NOT NULL AND e.currency <> f.currency)
    )
    + (
        SELECT count(*) FROM transfers t
        JOIN accounts s ON s.id = t.source_account_id
        JOIN accounts d ON d.id = t.destination_account_id
        WHERE t.currency <> s.currency OR t.currency <> d.currency
    )
    + (
        SELECT count(*) FROM fundings f
        JOIN accounts c ON c.id = f.account_id
        JOIN accounts treasury ON treasury.id = f.treasury_account_id
        WHERE f.currency <> c.currency OR f.currency <> treasury.currency
    )
"""
