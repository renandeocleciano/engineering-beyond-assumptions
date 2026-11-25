# Experiment 002: a transaction API under concurrency

Status: **recorded**. The API, the invariant tests, the first comparison, and the correctness remediation below are in the repository. Idempotency, asynchronous processing, and messaging are not part of this experiment.

Experiment 001 remains the counter study. Its code, tests, and recorded results are unchanged. This experiment adds fictional accounts, funding, transfers, and a signed ledger in the same PostgreSQL database. It does not use SQLite, floating-point money, currency conversion, or a payment provider.

## Problem

A committed database transaction can still be the wrong business result. Experiment 001 showed that for a counter. A transfer has a stricter requirement: the source debit, the destination credit, and the ledger rows must commit together, or not at all, and two concurrent debits must not spend the same funds.

## Hypotheses

1. Two concurrent transfers of 800 minor units from an account holding 1000 cannot both commit if the debit is conditional on the locked balance.
2. A failure after the debit and before the credit rolls the whole financial effect back.
3. Writing a previously read balance, without a lock or a conditional update, can commit every transfer and still leave an account balance that does not match the ledger.
4. Taking row locks in source-then-destination order can deadlock when other transactions move money in the opposite direction. Sorting the two account identifiers before locking removes that inversion in this two-account workflow.
5. A version check can detect a conflicting update. It does not by itself remove the row locks taken by the `UPDATE` statements.

These are claims about this schema and these workloads. They are not claims about every future transaction shape.

## Domain invariants

Amounts are integers in minor units. USD 10.50 is 1050. A transfer amount is strictly positive, and the source and destination differ. Both accounts are open customers in the same three-letter currency. The API does not check the letters against an ISO list and does not convert currencies.

Sign convention: a positive ledger amount credits a customer; a negative amount debits a customer. Treasury entries use the same signs and are allowed to be negative so that customer funding has a balancing row. A completed transfer has exactly two entries, one of `-amount` and one of `+amount`. A funding has the same shape between the treasury and the customer. The sum of all ledger amounts is zero when every movement was posted in pairs.

Account `balance_minor` is the projection. Reconciliation counts accounts whose balance differs from the sum of their entries, transfer or funding groups that do not sum to zero or do not contain two rows, and a nonzero ledger total. The public transfer path is expected to keep all of those counts at zero.

A rejected operation does not insert a transfer and does not change balances. Persisted transfers have status `completed`. There is no stored history of declined attempts.

## Architecture

The process is still one FastAPI application. `app/ledger` holds the HTTP contracts, the account and transfer service, the SQLAlchemy models, and the reconciliation queries. Routes commit once after the service returns. There is no repository framework and no message broker.

`GET /health` remains process liveness. It does not check the database.

| Method and path | Behavior |
| --- | --- |
| `POST /accounts` | Creates an open customer account at balance 0. `201`. |
| `GET /accounts/{account_id}` | Account metadata and current balance. `404` if missing. |
| `POST /accounts/{account_id}/fund` | Laboratory funding only. Credits the customer and debits the per-currency treasury. |
| `POST /transfers` | Synchronous transfer. `201` only after commit. |
| `GET /transfers/{transfer_id}` | A committed transfer. `404` if it was never committed. |
| `GET /accounts/{account_id}/entries` | Ledger entries for that account, `limit` 1–100 (default 50) and `offset`. |

Validation failures use `422`. A missing account or transfer uses `404`. Insufficient funds, a currency mismatch, and an account that cannot move money use `409`.

The service locks the two account rows in sorted UUID order and reloads them. It then applies each balance change with `balance_minor = balance_minor + delta`. A customer debit that would go negative matches no row and posts no ledger line. `after_debit` exists so a test can raise between the debit and the credit. The HTTP route does not pass it.

Funding creates the treasury row for a currency on first use, inside a savepoint, so a concurrent insert does not abort the outer transaction.

Ledger `UPDATE` and `DELETE` are rejected by the trigger `ledger_entries_no_mutation`. `TRUNCATE` does not fire that row trigger. The application never updates or deletes entries. Database ownership can still remove them.

## What was already in the repository

Before this experiment the application was one FastAPI module, one engine, and a `counters` table. Sessions do not commit themselves. Alembic head was revision `001`. The CI workflow directory was empty, so a push would not have run the PostgreSQL tests. Experiment runners talk to PostgreSQL directly and do not import the ledger. The local database is published on port **5434** because another PostgreSQL instance already held 5432. CI uses port 5432 inside the runner. Financial tests truncate `ledger_entries`, `fundings`, `transfers`, and `accounts`. They do not truncate `counters`.

## Strategies

Strategies A–D live under `experiments/experiment_002/strategies.py` and are not mounted on the public transfer route. The route calls `execute_transfer`. The corrected benchmark measures that function separately from the raw-SQL lock strategy.

| Strategy | What it does | Where it runs |
| --- | --- | --- |
| A — naive | Reads balances, then writes the absolute values it computed. | Experiment only. |
| B — conditional | `UPDATE … SET balance = balance - amount WHERE balance >= amount`, source first, then credits the destination. No lock reordering. | Experiment only. |
| C — `pessimistic_sql` | `SELECT … FOR UPDATE` in sorted identifier order, then writes absolute balances and the ledger pair. The function inside `strategies.py` is still named pessimistic. | Benchmark only. |
| D — optimistic | Compares `version`, then updates both rows. Retries the whole transaction up to 5 times on a version conflict, with no delay. Insufficient funds is a rejection, not a retry. Other database errors are not retried. | Experiment only. |
| Service — `pessimistic_service` | `execute_transfer` in a new SQLAlchemy session per operation. | The public path. The corrected benchmark calls this function directly. It does not go through HTTP. |

No strategy was excluded. Retry with backoff, idempotency keys, and an outbox are later experiments.

## Test methodology

Integration tests in `tests/test_ledger.py` use the Compose PostgreSQL, not SQLite. They are marked `postgres` and skip unless `DATABASE_URL` is set.

- Fund, transfer, list entries, and expect reconciliation violations of 0.
- Reject the same account, a currency mismatch, and insufficient funds. The insufficient-funds case leaves the balance unchanged.
- Concurrent overspend: one account starts at 1000; two transfers of 800 run after a barrier. Exactly one commits. The source ends at 200.
- Four concurrent transfers of 100 from an account holding 1000 all commit. The source ends at 600.
- A raised hook after the debit rolls the transaction back. No transfer row remains.
- `UPDATE` and `DELETE` on `ledger_entries` raise. An injected balance change is visible to reconciliation and is then rolled back.
- Ten transfers each way between the same two accounts. The API lock order leaves both balances at their funded value. `pg_stat_database.deadlocks` does not increase during that test.
- Two sessions load the same balance, wait before either takes a lock, then both fund. The balance matches the ledger.
- A session that already loaded an account cannot spend funds a second transaction has already moved.
- A concurrent fund and transfer that both loaded the old balance still reconcile.
- Planted rows for a missing ledger, a short or uneven movement, the wrong accounts, a currency mismatch, and a customer or treasury balance drift are visible to `invariant_report` and are rolled back.

The last test shows that this opposite-transfer workflow did not deadlock. It does not prove that every future statement order is safe.

The full suite, including Experiment 001, was run after the ledger tests existed: Ruff, mypy, and pytest. **23 passed** on 2026-11-25 with `DATABASE_URL` pointing at `localhost:5434`.

## Benchmark methodology

The runner is `experiments/experiment_002/benchmark.py`. It truncates the financial tables, not `counters`. The application container was stopped. Isolation is the session default, `READ COMMITTED`. Each cell has 4 workers, 10 warmup iterations per worker, 25 measured iterations per worker, amount 100, and 3 repetitions. Warmup is excluded from logical operations, throughput, and latency. Latency percentiles use successful logical operations only: p50 is the median, p95 and p99 are the inclusive nearest rank, and p99 is omitted when fewer than 100 operations succeeded. Throughput is committed transfers divided by the measured window.

`logical_operations` is `workers * iterations` (100). `database_attempts` adds retries. `deadlocks` is the change in `pg_stat_database.deadlocks` for the whole cell, warmup included. `errors_by_sqlstate` counts measured logical operations only. A strategy with an invariant violation is not ranked above a slower strategy that preserved the invariants.

**Hot source.** One source, one destination per worker, funded with `4 * (10 + 25) * 100 = 14000` so every measured transfer can succeed. No `lock_timeout` was set. Recorded at 2026-11-25 22:10:24–22:10:27 UTC. PostgreSQL 17.11. Twelve JSON files: `r{1,2,3}-{naive,conditional,pessimistic,optimistic}.json`.

**Opposite direction.** Two accounts, half the workers transfer A to B and half transfer B to A. Each account is funded with `2 * 35 * 100 = 7000`. Each worker connection sets `lock_timeout` to 2 seconds, which is above the server `deadlock_timeout` of 1 second. A detected deadlock is SQLSTATE `40P01`. A wait that reaches 2 seconds without a detected deadlock is SQLSTATE `55P03`. Recorded at 2026-11-25 22:13:47–22:23:28 UTC. Twelve JSON files: `r{1,2,3}-opposite-{strategy}.json`.

An earlier opposite attempt without `lock_timeout` produced no JSON. Four sessions were still waiting on tuple and transaction locks, with `deadlock_timeout` at 1 second, when the run was inspected. That process was terminated. PostgreSQL checks for a deadlock after `deadlock_timeout` and, if it does not find one yet, can wait until a lock timeout exists. The recorded opposite matrix uses the 2 second limit so the run can finish. Those hung sessions are not included in the counts below.

The client is the host Python process through the published port 5434. The virtual machine size was not re-measured for this run. JSON is written under `benchmark-results/experiment_002/`, which is gitignored.

## Results

### Hot source

Every naive, conditional, and pessimistic cell committed 100 of 100 measured transfers, with 0 retries, 0 exhausted retries, 0 deadlocks, and an empty SQLSTATE map. Optimistic committed 79 in every repetition, retried 116, 114, and 114 times, exhausted 21 logical operations in every repetition, and took 216, 214, and 214 database attempts. Its failures were version conflicts, not SQLSTATE errors. Rejected transfers were 0 in every cell.

Invariant violations were 0 for conditional, pessimistic, and optimistic. Naive committed all 100 transfers and still had `balance_mismatches = 1` in every repetition. The ledger pairs still summed to zero. The lost update is in the balance projection. That throughput is not a better result.

Successful-operation latency and throughput on this machine:

| Strategy | Committed | Violations | Transfers/s | p50 ms | p95 ms | p99 ms |
| --- | --- | --- | --- | --- | --- | --- |
| Naive | 100, 100, 100 | 1, 1, 1 | 503, 618, 624 | 7.16, 4.66, 5.43 | 13.55, 11.31, 10.92 | 17.20, 14.33, 18.16 |
| Conditional | 100, 100, 100 | 0 | 561, 645, 624 | 6.84, 4.67, 5.98 | 12.34, 12.03, 10.52 | 14.17, 15.36, 12.80 |
| Pessimistic | 100, 100, 100 | 0 | 517, 506, 504 | 6.04, 7.87, 7.89 | 12.99, 8.65, 9.73 | 19.44, 10.00, 10.35 |
| Optimistic | 79, 79, 79 | 0 | 383, 387, 389 | 2.60, 2.52, 2.56 | 4.87, 3.49, 3.12 | omitted (79 samples) |

Optimistic p50 is lower because the 21 exhausted operations are excluded from the latency sample. The retry loop also has no backoff, so a conflict is retried immediately.

### Opposite direction

Pessimistic committed 100 of 100 in every repetition, with 0 errors, 0 deadlocks, and 0 invariant violations. Throughput was 443, 445, and 429 transfers/s. p50 was 9.15, 9.07, and 9.30 ms. p99 was 11.34, 11.74, and 11.74 ms.

The other strategies committed only some of the 100 measured operations. Deadlock counts are higher than the measured `40P01` counts because the database counter includes warmup.

| Strategy | Committed | `40P01` | `55P03` | Deadlock delta | Violations |
| --- | --- | --- | --- | --- | --- |
| Naive | 36, 33, 38 | 48, 54, 48 | 16, 13, 14 | 67, 73, 67 | 2, 2, 2 |
| Conditional | 34, 30, 38 | 48, 49, 53 | 18, 21, 9 | 67, 68, 73 | 0 |
| Pessimistic | 100, 100, 100 | 0 | 0 | 0 | 0 |
| Optimistic | 31, 33, 44 | 39, 37, 32 | 30, 30, 19 | 54, 51, 42 | 0 |

Naive again preserved balanced ledger pairs and still mismatched both account projections (`balance_mismatches = 2`). Conditional and optimistic kept violations at 0 by aborting conflicting transactions. Optimistic still deadlocked: the two `UPDATE` statements lock the source row and then the destination row, and the opposite worker does the reverse. A version column does not choose a lock order.

Successful naive and conditional operations in this scenario waited about 2 seconds (p50 around 2015–2019 ms, except optimistic repetition 3, whose p50 was 20.05 ms and whose p95 was 3014.84 ms). That latency includes waiting on the other transaction’s deadlock or lock timeout. p99 is omitted because fewer than 100 operations succeeded. Their throughput, about 0.5–1.1 committed transfers/s, is the cost of those aborts and waits, not evidence of a faster correct design.

## Correctness remediation

An audit of the first implementation found that `fund_account` loaded the customer, then locked it, and `_post` added the amount to the in-memory balance. SQLAlchemy does not refresh a loaded object on a later `SELECT` unless `populate_existing` is set. The locked row could be current while the object still held the earlier balance, and the flush wrote that stale value back.

Before the fix, a PostgreSQL test loaded balance 500 in two sessions, waited on barriers that run before `SELECT FOR UPDATE`, and funded 100 in each session. Both funding rows committed. The customer ledger summed to 700 and there were 3 funding rows and 3 customer entries. The stored balance was 600. The assertion that the balance was 700 failed. A separate script printed `{'balance': 600, 'ledger': 700, 'fundings': 3, 'customer_entries': 3}`.

The service now reloads locked accounts and applies `balance_minor = balance_minor + delta`. A customer debit that would go negative matches no row, raises insufficient funds, and does not insert that ledger line. The same test, a transfer that uses a previously loaded balance, and a concurrent fund plus transfer now pass and reconcile. The full suite after the change was 28 passed, with Ruff and mypy clean and Alembic at revision `002`. Experiment 001's modules were not edited.

Reconciliation now separates customer and treasury balance mismatches and reports `balance_discrepancy_minor`, the sum of absolute gaps. It also counts transfers and fundings with no entries, movements whose entries have the wrong count or do not sum to zero, entries on the wrong accounts, and currency mismatches. Database constraints still do not require a balanced pair. The report detects that. It does not enforce it on write.

The corrected benchmark keeps every original strategy and adds `pessimistic_service`, which calls `execute_transfer` in a fresh session. The raw-SQL lock strategy is labeled `pessimistic_sql`. The first 24 JSON files under `benchmark-results/experiment_002/` were left in place. The new files are under `benchmark-results/experiment_002_corrected/` and use `"phase": "experiment-002-corrected"`. Warmup counts and warmup deadlock deltas are separate from the measured window. Each result has success latency and end-to-end latency. A cell with invariant violations makes the CLI exit 1. A 4-worker, 5-iteration naive hot-source invocation exited 1. That invocation is not one of the saved cells.

The corrected matrix ran on 2026-11-25 22:46:00–22:57:08 UTC against PostgreSQL 17.11, `READ COMMITTED`, `deadlock_timeout` 1s. The host reported 10 CPUs. Docker reported 2 CPUs and 2054565888 bytes of memory. The application container was stopped. The original-size workload was 4 workers, 10 warmup iterations, 25 measured iterations, amount 100, 3 repetitions, both scenarios, and all five strategies. Opposite transfers set `lock_timeout` to 2 seconds. The larger workload used 20 warmup iterations and 100 measured iterations. Its hot-source cells used all five strategies. Its opposite cells used only `pessimistic_sql` and `pessimistic_service`, because the unordered strategies spend their time in deadlock and lock-timeout waits.

On the original-size hot source, naive committed all 100 measured transfers in every repetition and left a final absolute discrepancy of 10500 minor units in every repetition. `unbalanced_transfers` was 0 and `ledger_sum` was 0. The discrepancy includes warmup, because it is computed from the final rows. The three larger naive hot-source repetitions each ended at 36000. One of those three increased the measured-window deadlock counter by 1 while `errors_by_sqlstate` stayed empty and all 400 measured operations committed. The recorded errors do not explain that counter. The other two larger naive repetitions had a measured deadlock delta of 0.

`pessimistic_sql` and `pessimistic_service` committed every measured operation, with discrepancy 0 and deadlock delta 0, in the original-size hot and opposite cells and in the larger cells. On the original-size hot source their successful throughput was 437–474 transfers/s for SQL and 343–402 transfers/s for the service. On the original-size opposite workload it was 455–508 for SQL and 378–390 for the service. The larger opposite cells were 504–516 for SQL and 431–433 for the service. Both latency distributions match within each of these cells because every operation committed. The SQL path was faster here. That is a performance difference on this machine, not a correctness ranking.

Conditional updates still reconciled and still produced `40P01` and `55P03` on opposite transfers. Optimistic hot-source cells still preserved the ledger and still exhausted part of the work: 21 of 100 measured operations in each original-size repetition, and 88 of 400 in each larger repetition. For the first original-size optimistic hot-source cell, success-only p99 is omitted because only 79 operations committed, while end-to-end p99 is 17.85 ms because the failures are in that sample. Comparing those strategies by success latency alone would hide the unfinished work.

## Known limitations

- The recorded comparisons are one machine, two workloads, and three repetitions. They are not a capacity number.
- `pessimistic_service` measures `execute_transfer`. It does not measure HTTP, authentication, or a client round trip. `pessimistic_sql` remains a separate implementation.
- Rejected transfers are not stored, so a decline cannot be fetched later.
- Optimistic retries have no delay and a fixed cap of 5. Exhausted hot-source operations are unfinished work, not proof that the books were corrupted.
- In the first JSON files, `deadlocks` includes warmup. The corrected files report warmup and measured deadlock deltas separately. A deadlock counter can still move without a recorded `40P01`, as in one larger naive cell.
- `TRUNCATE` bypasses the immutability trigger.
- The treasury balance is allowed to be negative. That is the balancing entry for laboratory funding, not a customer overdraft.
- Passing the opposite-transfer test, or the pessimistic benchmark cells, does not show that every future statement order is deadlock-free.
- Idempotency keys are not implemented. A repeated `POST /transfers` is a second transfer.

## Reproduction

From the repository root, with the database on port 5434:

```sh
docker compose up -d db
export DATABASE_URL='postgresql+psycopg://experiments:experiments@localhost:5434/experiments'
alembic upgrade head
ruff check .
mypy
pytest

docker compose stop app
python -m experiments.experiment_002.benchmark --scenario hot-source --strategy pessimistic_service
python -m experiments.experiment_002.benchmark --scenario opposite --strategy pessimistic_service
docker compose start app
```

The benchmark commands print one cell and exit 1 when that cell has an invariant violation. The first matrix is under `benchmark-results/experiment_002/`. The corrected matrix is under `benchmark-results/experiment_002_corrected/`. The benchmark truncates laboratory financial tables. Use a disposable database.
