# ADR 0003: Lock accounts in identifier order and apply relative balance updates

Status: Accepted

A transfer or funding changes money in one PostgreSQL transaction. The public path locks the affected account rows with `SELECT … FOR UPDATE` in sorted account-identifier order. The query uses `populate_existing`, so an account already loaded in the session does not keep a stale balance, status, or currency. Identifier order removes the lock-order inversion that opposite transfers otherwise create. It does not make every future workflow deadlock-free.

The balance write is separate from that lock. It is `UPDATE accounts SET balance_minor = balance_minor + :delta`. A customer debit also requires `balance_minor + :delta >= 0`. A treasury debit does not, because laboratory funding posts the negative amount on the treasury. If the customer predicate matches no row, the service raises insufficient funds and does not insert the ledger line for that post. The caller's transaction then rolls back, including a transfer or funding row flushed earlier in the same transaction. The lock orders and refreshes the rows. The relative update is what commits the balance. Neither one replaces the other.

Customer balances are projections. A ledger credit is a positive integer in minor units. A debit is the same amount with a negative sign. A completed transfer posts the negative amount on the source and the positive amount on the destination. There is no currency conversion.

Rejected requests do not insert a committed transfer. A successful HTTP response is returned only after commit. The application does not update or delete ledger rows. A `BEFORE UPDATE OR DELETE` trigger rejects those statements. The table owner can still `TRUNCATE` the table. Application immutability is not a substitute for database privileges.

Database constraints reject a negative customer balance, a non-positive movement, and a zero ledger amount. They do not require two balancing entries, the movement's own accounts, or a matching currency. `invariant_report` detects those breaks, including a transfer or funding with no entries and the absolute gap between a balance and its ledger sum. Reconciliation reports failures. It does not enforce them on write.

The strategies under `experiments/experiment_002/` are not the HTTP handler. The handler is `app.ledger.service.execute_transfer`. The benchmark calls that function as `pessimistic_service` and keeps the raw-SQL lock strategy as `pessimistic_sql`. A result that commits more transfers while breaking a balance or ledger invariant is not a better design. A run that reports invariant violations exits nonzero.
