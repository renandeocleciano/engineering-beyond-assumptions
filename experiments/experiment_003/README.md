# Experiment 003 — Idempotency Under Concurrency and Failure

**Status:** recorded (publication evidence set, 2026-11-27)

**Question:** When a laboratory transfer commits but the client does not receive the response—or sends concurrent duplicates—what prevents a second financial movement?

**Money disclaimer:** All amounts are fictional laboratory units. This is not a payment product.

## Problem statement

A successful `POST /transfers` means PostgreSQL committed the transfer and ledger entries. If the HTTP response is lost, the client cannot tell from a timeout alone whether money moved. Without idempotency, a retry can create a second transfer. Both transfers can reconcile individually. The bug is at **business-operation identity**.

## Hypotheses

1. Without coordination, sequential retries after a lost response duplicate the transfer amount.
2. A naive `SELECT` then `INSERT` without a uniqueness wait path can still duplicate under concurrency.
3. A unique `(scope, key)` row reserved and completed in the **same** transaction as the transfer yields at most one committed movement for equivalent requests, including after lost responses.
4. Same key with a different fingerprint rejects without further movement.
5. Idempotency adds measurable latency overhead versus unique-key baseline traffic.

## System architecture

```text
Client
  |  Idempotency-Key + transfer body
  v
POST /transfers  (FastAPI)
  |
  v
execute_idempotent_transfer  (one SQLAlchemy / PostgreSQL transaction)
  1. INSERT idempotency_records (unique scope+key) via savepoint
  2. execute_transfer (ordered FOR UPDATE, relative balances, ledger)
  3. mark record completed + transfer_id
  4. COMMIT
  |
  v  (optional test-only fault: after_commit_before_response)
HTTP 201 (new) or 200 (replay)
```

Unsafe strategies live only under `experiments/experiment_003/unsafe.py` and are not public routes.

## Idempotency contract

| Case | Behavior |
| --- | --- |
| A — New key | Execute once; commit record + transfer; `201` |
| B — Same key, same fingerprint | No new transfer; `200` + `idempotent_replay=true` |
| C — Same key, different fingerprint | `409`; no new movement |
| D — Concurrent same key | One commit; others block on unique index then replay |
| E — Commit, lost response | Retry replays original |
| F — Rollback before commit | Reservation and transfer vanish; retry may execute |
| G — Business rejection | Reservation rolls back; retry may reevaluate balances |

**Fingerprint:** SHA-256 of canonical JSON with sorted keys:
`amount_minor`, `currency` (upper), `destination_account_id`, `operation=transfer`, `scope=lab.transfer`, `source_account_id`.

**Key format:** 8–128 chars of `A-Za-z0-9._:-`.

**Scope / tenancy (non-production):** Keys are laboratory-wide under `lab.transfer`. There is **no authentication**. Any caller who reuses another party’s key can receive that party’s committed transfer on replay (cross-client information exposure). Do not treat this as a multi-tenant payments design.

**Migration from unkeyed API:** Experiment 003 makes the header required. There is no silent legacy path.

ADR: [0004-transfer-idempotency.md](../../docs/adr/0004-transfer-idempotency.md)

Independent audit: [experiment-003-technical-evidence-report.md](../../docs/experiment-003-technical-evidence-report.md)

## PostgreSQL constraints

- `uq_idempotency_scope_key` on `(scope, idempotency_key)`
- Partial unique index on `transfer_id` when not null
- Check that reserved rows have null `transfer_id` / `completed_at`; completed rows require both

Existing Experiment 002 protections remain: ordered account locks, `populate_existing`, relative conditional debits, ledger immutability triggers, reconciliation.

## Failure injection

`app.ledger.faults` registers callables at:

- `before_idempotency_reservation`
- `after_reservation_before_transfer`
- `after_debit_before_credit`
- `before_commit`
- `after_commit_before_response`

Never enabled by public HTTP. Cleared by the laboratory pytest fixture.

Recoverable through retry with the **same** key: post-commit lost response. Pre-commit faults roll back; retry executes as a new attempt.

## Test isolation

`app/laboratory.py` truncates financial tables only when `DATABASE_URL` targets host `localhost`/`127.0.0.1`/`db`/`::1` and database name `experiments`. Other URLs raise. Fixtures truncate before and after each postgres client test so residual ledger violations cannot leak across runs.

## Benchmark methodology

Runner: `experiments/experiment_003/benchmark.py`

Strategies:

- `safe` — protected path (`execute_idempotent_transfer`)
- `unsafe_retry` — always `execute_transfer` (expected duplicates on retry)
- `naive_check` — check-then-insert without uniqueness (expected concurrent duplicates)

Scenarios: `baseline`, `sequential_retries`, `concurrent_duplicate_storm`, `mixed_traffic`, `failure_recovery`.

**Measurement rules (schema v2):**

- Benchmarks call the **service layer**, not HTTP. Pytest covers HTTP semantics.
- Each published cell is **one recorded run** (no multi-run statistics).
- For traffic scenarios (`baseline`, `sequential_retries`, `mixed_traffic`): warmup operations run first; **attempt and latency metrics exclude warmup**; `committed_transfers` is a final table count and **includes warmup rows** when `warmup > 0` (typically 2 warmup + 40 measured ⇒ 42 rows on unique-key cells).
- Throughput fields:
  - `service_attempts_per_second` — measured service attempts / elapsed
  - `committed_transfer_rows_per_second` — all transfer rows / elapsed (includes duplicates and warmup rows)
  - `unique_logical_successes_per_second` — measured-window unique logical successes / elapsed
- The initial field name `unique_transfers_per_second` (v1 only) meant committed transfer **rows** and must not be read as unique business operations.

The runner exits nonzero if `safe` reports duplicate transfer effects or if reconciliation finds violations. Unsafe duplicates are classified as expected findings.

### Evidence locations

| Path | Contents |
| --- | --- |
| [`results/v2-publication/`](results/v2-publication/) | Publication JSON (schema v2) |
| [`results/v1-initial/`](results/v1-initial/) | Original 2026-11-27 JSON preserved for audit continuity |
| [`results/README.md`](results/README.md) | Version notes |

### Reproduction

```sh
export DATABASE_URL='postgresql+psycopg://experiments:experiments@localhost:5434/experiments'
alembic upgrade head

OUT=experiments/experiment_003/results/v2-publication
mkdir -p "$OUT"

python -m experiments.experiment_003.benchmark \
  --scenario baseline --strategy safe \
  --workers 1 --warmup 2 --iterations 40 --seed 42 \
  --output "$OUT/baseline-safe.json"

python -m experiments.experiment_003.benchmark \
  --scenario baseline --strategy unsafe_retry \
  --workers 1 --warmup 2 --iterations 40 --seed 42 \
  --output "$OUT/baseline-raw.json"

python -m experiments.experiment_003.benchmark \
  --scenario sequential_retries --strategy safe \
  --workers 1 --warmup 2 --iterations 40 --duplicate-rate 1.0 --seed 42 \
  --output "$OUT/sequential-safe.json"

python -m experiments.experiment_003.benchmark \
  --scenario sequential_retries --strategy unsafe_retry \
  --workers 1 --warmup 2 --iterations 40 --duplicate-rate 1.0 --seed 42 \
  --output "$OUT/sequential-unsafe.json"

python -m experiments.experiment_003.benchmark \
  --scenario concurrent_duplicate_storm --strategy safe \
  --workers 8 --warmup 0 --iterations 1 --seed 42 \
  --output "$OUT/storm-safe.json"

python -m experiments.experiment_003.benchmark \
  --scenario concurrent_duplicate_storm --strategy naive_check \
  --workers 8 --warmup 0 --iterations 1 --seed 42 \
  --output "$OUT/storm-naive.json"

python -m experiments.experiment_003.benchmark \
  --scenario mixed_traffic --strategy safe \
  --workers 1 --warmup 2 --iterations 40 --duplicate-rate 0.3 --seed 42 \
  --output "$OUT/mixed-safe.json"

python -m experiments.experiment_003.benchmark \
  --scenario failure_recovery --strategy safe \
  --workers 1 --warmup 0 --iterations 20 --retry-budget 3 --seed 42 \
  --output "$OUT/failure-safe.json"

python -m experiments.experiment_003.benchmark \
  --scenario failure_recovery --strategy unsafe_retry \
  --workers 1 --warmup 0 --iterations 20 --retry-budget 3 --seed 42 \
  --output "$OUT/failure-unsafe.json"
```

## Actual results (v2-publication)

Single-pass service-level run on 2026-11-27 against PostgreSQL 17.11, Python 3.13.11,
SQLAlchemy 2.1.9, seed 42. Absolute RPS varies slightly between machine passes; **correctness
counts below are the publication set**.

Warmup: traffic cells use `--warmup 2` / `--iterations 40`, so `committed_transfers` often
equals **42** on unique-key safe/raw baselines (2 warmup + 40 measured). Storm and failure
cells use warmup 0.

| File | Transfers (incl. warmup) | Duplicates | Deduped | Recovered | Service attempts/s | p50 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline-raw | 42 | 0 | 0 | 0 | 334.656 | 2.934 |
| baseline-safe | 42 | 0 | 0 | 0 | 223.445 | 4.477 |
| sequential-safe | 42 | 0 | 40 | 0 | 300.275 | 3.370 |
| sequential-unsafe | 84 | 40 | 0 | 0 | 350.562 | 2.865 |
| storm-safe | 1 | 0 | 7 | 0 | 36.536 | 28.124 |
| storm-naive | 8 | 7 | 0 | 0 | 24.550 | 38.837 |
| mixed-safe | 42 | 0 | 13 | 0 | 246.312 | 4.374 |
| failure-safe | 20 | 0 | 20 | 20 | 247.772 | 4.124 |
| failure-unsafe | 40 | 20 | 0 | 0 | 318.849 | 3.028 |

Invariant violations were 0 in every cell. Exact fields are in the JSON files.

**Baseline overhead comparison:** one recorded service-level pair (`baseline-raw` vs
`baseline-safe`), not HTTP throughput and not a multi-run confidence interval. On this
pass, safe was slower (~223 vs ~335 service attempts/s). Treat as an observed laboratory
delta, not a capacity rating. The gap is consistent with extra fingerprinting, idempotency
insert, and unique-index coordination, but this harness does not isolate statement-level
DB time.

**Correctness takeaway:** protected retries and concurrent duplicate storms produced one
transfer per logical key. Unsafe retries and the naive check-then-insert race duplicated
money movement while still reconciling ledger invariants per transfer.

## Known limitations

- Laboratory-wide key scope with **no authentication**; key reuse can expose another caller’s transfer on replay.
- Concurrent duplicates wait on the unique index rather than returning an explicit in-progress HTTP status.
- Benchmarks are **service-level**, **single-run**; HTTP semantics are covered by pytest.
- Warmup rows are included in `committed_transfers` for traffic scenarios.
- The naive storm uses an explicit post-lookup barrier so the race is deterministic.
- Host/container limits in JSON are observational; numbers are not capacity claims.
- Lost-response faults are in-process exceptions after commit, not real TCP disconnects.
- Not safe for real financial production use.

## Quality gates

```sh
ruff check app tests experiments alembic
mypy
pytest
pytest   # second pass: isolation check
```
