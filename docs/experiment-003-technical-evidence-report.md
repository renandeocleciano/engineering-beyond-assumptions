# Experiment 003 — Independent Technical Evidence Report

**Document type:** Independent technical evidence report  
**Subject:** Experiment 003 — Idempotency Under Concurrency and Failure  
**Repository:** `engineering-beyond-assumptions`  
**Local path:** `/Users/renandeocleciano/Projetos/engineering-beyond-assumptions`  
**Report date:** 2026-11-27  
**Author role:** Independent Principal Backend Engineer / Technical Auditor  

This report is intended for a senior engineer without repository access. Every claim is classified as **observed measurement**, **source-code guarantee**, **automated-test evidence**, **architectural assumption**, **known limitation**, or **Not verified**.

**No application code, migrations, tests, benchmarks, or previous experiment artifacts were modified while producing this report.** Read-only verification commands were executed (Ruff, Mypy, Alembic current, pytest twice, laboratory URL guard checks, live HTTP status-shape probe against the disposable laboratory database, and inspection of recorded JSON under `benchmark-results/experiment_003/`).

### Publication-prep errata (after this audit)

A later documentation pass (still without changing idempotency or financial semantics) addressed audit findings:

1. **Metric naming.** The field formerly called `unique_transfers_per_second` counted **all committed transfer rows** (including duplicates and warmup). Publication schema v2 replaces it with `committed_transfer_rows_per_second` and adds `unique_logical_successes_per_second` / `service_attempts_per_second`.
2. **Evidence paths.** Historical audit JSON is preserved at `experiments/experiment_003/results/v1-initial/`. Publication JSON is at `experiments/experiment_003/results/v2-publication/`. Tables in §§9–11 of this report cite the original v1 filenames/paths under `benchmark-results/experiment_003/`; correctness counts match v2. Absolute RPS may differ slightly across re-runs.
3. **Warmup / HTTP.** Traffic cells: attempt and latency metrics exclude warmup; `committed_transfers` includes warmup rows. Baseline overhead is one service-level recorded pair, not HTTP throughput.

---

## 1. Executive summary

Experiment 003 extends the Experiment 002 Transaction API so that `POST /transfers` requires an `Idempotency-Key`. The protected path inserts an `idempotency_records` row and the financial transfer in **one PostgreSQL transaction**, coordinated by a unique constraint on `(scope, idempotency_key)`.

**Independent quality re-run (this report):**

| Check | Result |
| --- | --- |
| Ruff | Passed |
| Mypy | Passed (38 source files) |
| Alembic | `003 (head)` |
| Pytest run 1 | **43 passed** |
| Pytest run 2 | **43 passed** |
| Postgres marker | **36 passed**, 7 deselected (not skipped) |

**Recorded benchmarks (gitignored JSON, dirty tree, SHA `ccdff1d`):** protected sequential and concurrent duplicate workloads reported **0** `duplicate_transfer_effects` and **0** invariant violations. Unsafe sequential retries reported **40** duplicates. Naive concurrent storm reported **8** transfers / **7** duplicates. Protected failure-recovery reported **20/20** recovered lost responses with **1** transfer per logical operation.

**Publication readiness:** **READY WITH QUALIFICATIONS** (see §14). This is a laboratory result, not a production payments certification.

---

## 2. Implementation and environment

### 2.1 Git state

| Field | Value |
| --- | --- |
| `HEAD` | `ccdff1d468173eb91673821b503914f704aed054` |
| Branch | `main` tracking `origin/main` |
| Working tree | **Dirty** — Experiment 003 is **uncommitted** |
| Benchmark JSON `git_commit_sha` | `ccdff1d…` with `"working_tree_dirty": true` |

**Implication:** There is **no git commit SHA that uniquely identifies the Experiment 003 implementation**. Reviewers must treat the working tree (and this report’s path/line citations) as the evidence set. Historical Experiment 001/002 evidence files were not rewritten during this audit.

### 2.2 Files introduced or modified for Experiment 003

**New**

| Path | Role |
| --- | --- |
| `alembic/versions/003_create_idempotency_records.py` | Schema revision 003 |
| `app/laboratory.py` | Guarded laboratory truncate helpers |
| `app/ledger/fingerprint.py` | Key validation + SHA-256 fingerprint |
| `app/ledger/faults.py` | Test-only fault injection registry |
| `app/ledger/idempotency.py` | `execute_idempotent_transfer` |
| `docs/adr/0004-transfer-idempotency.md` | ADR |
| `experiments/experiment_003/` | README, `benchmark.py`, `unsafe.py` |
| `tests/conftest.py` | Shared laboratory fixtures |
| `tests/test_idempotency.py` | Exp 003 integration tests |
| `tests/test_experiment_003_unsafe.py` | Unsafe baseline tests |

**Modified**

| Path | Change summary |
| --- | --- |
| `README.md` | Catalog + Idempotency-Key note |
| `alembic/env.py` | Import `IdempotencyRecord` |
| `app/ledger/api.py` | Required header; idempotent handler |
| `app/ledger/errors.py` | `IdempotencyKeyConflict` / `Required` |
| `app/ledger/models.py` | `IdempotencyRecord` model |
| `tests/test_ledger.py` | Transfer helpers send `Idempotency-Key` |

**Unchanged (still in force):** `app/ledger/service.py` financial path (`execute_transfer`), `app/ledger/reconcile.py`, Experiment 001/002 runners and historical `benchmark-results/experiment_00{1,2}*/` evidence.

### 2.3 Versions (measured on this host)

| Component | Value |
| --- | --- |
| Python | 3.13.11 |
| FastAPI | 0.165.0 |
| SQLAlchemy | 2.1.9 |
| Alembic | 1.20.1 |
| psycopg | 3.3.9 |
| PostgreSQL | 17.11 (Debian 17.11-1.pgdg13+2) |
| Default isolation | `read committed` |
| `deadlock_timeout` | `1s` |
| `lock_timeout` | `0` (disabled) |
| Host | Darwin 25.0.0 arm64 |
| Host CPUs (from benchmark JSON `os.cpu_count`) | 10 |
| Host RAM / Docker limits | **Not re-measured in this audit session** (sysctl/docker introspection blocked); Exp 002 report previously recorded ~16 GiB host RAM and Docker ~2 CPU / ~1.91 GiB |

### 2.4 Application vs benchmark environments

| Concern | HTTP / pytest | Benchmark runner |
| --- | --- | --- |
| Process | Host `.venv` + FastAPI `TestClient` (in-process ASGI) | Host `.venv` calling service functions |
| DB URL | `postgresql+psycopg://experiments:experiments@localhost:5434/experiments` | Same |
| Isolation | PostgreSQL default `READ COMMITTED` | Documented as `READ COMMITTED` in JSON |
| HTTP stack | Exercised in `tests/test_idempotency.py` | **Not** measured for RPS; service-layer attempts only |

### 2.5 Experiment 002 protections that remain

**Source-code guarantee / prior evidence:**

1. Accounts locked in sorted UUID order with `FOR UPDATE` + `populate_existing` (`app/ledger/service.py`).
2. Relative balance updates (`balance_minor = balance_minor + delta`) with conditional customer debit predicate.
3. Transfer + two ledger entries in the same caller transaction.
4. Ledger immutability trigger; reconciliation detectors in `app/ledger/reconcile.py`.
5. Public protected transfers call `execute_transfer` **inside** `execute_idempotent_transfer` — they do not replace the financial path.

### 2.6 Reproduction commands

```sh
export DATABASE_URL='postgresql+psycopg://experiments:experiments@localhost:5434/experiments'
alembic upgrade head
ruff check app tests experiments alembic
mypy
pytest
pytest   # isolation second pass
pytest -m postgres --collect-only

mkdir -p benchmark-results/experiment_003
python -m experiments.experiment_003.benchmark \
  --scenario baseline --strategy safe \
  --workers 1 --warmup 2 --iterations 40 --seed 42 \
  --output benchmark-results/experiment_003/baseline-safe.json
# … remaining cells as in experiments/experiment_003/README.md
```

---

## 3. Idempotency contract

### 3.1 Header and fingerprint

| Rule | Definition |
| --- | --- |
| Header | `Idempotency-Key` required on `POST /transfers` |
| Key pattern | `^[A-Za-z0-9._:-]{8,128}$` (`app/ledger/fingerprint.py`) |
| Scope | Constant `lab.transfer` — laboratory-wide; **no tenant / auth model** |
| Fingerprint | SHA-256 hex of canonical JSON with sorted keys: `amount_minor`, `currency` (uppercased), `destination_account_id`, `operation=transfer`, `scope`, `source_account_id` |
| Non-goals | Digest is **not** authentication; fingerprint is **not** the sole operation id |

### 3.2 HTTP behavior (live probe + tests)

Independent live probe on the laboratory DB (2026-11-27), same key `audit-shape-0001`:

| Case | Status | Body highlights |
| --- | --- | --- |
| New key + valid payload | **201** | `idempotent_replay: false`, transfer `id` present |
| Same key + identical payload | **200** | Same `id`, `idempotent_replay: true` |
| Same key + different `amount_minor` | **409** | `{"detail":"Idempotency-Key was reused with a different payload"}` |
| Missing header | **400** | `{"detail":"Idempotency-Key header is required"}` |

Handler mapping (`app/ledger/api.py`):

| Condition | Status |
| --- | --- |
| Missing / invalid key | 400 |
| Account not found | 404 |
| Insufficient funds / currency / unavailable / key conflict | 409 |
| Same-account validation (Pydantic) | 422 |
| First success | 201 |
| Replay | 200 |

### 3.3 Case matrix (contract vs evidence)

| Case | Documented behavior | Evidence |
| --- | --- | --- |
| A — New key | Execute once; commit record + transfer; 201 | `test_sequential_duplicate_http`; live probe |
| B — Same key, same fingerprint | No new transfer; 200 replay | Same |
| C — Same key, different fingerprint | 409; no new movement | `test_payload_conflict_http`; live probe |
| D — Concurrent same key | One commit; others wait then replay | `test_concurrent_duplicate_same_key` (service); `storm-safe.json` |
| E — Commit, lost response | Retry replays original | `test_lost_response_after_commit_http`; `failure-safe.json` 20/20 |
| F — Rollback before commit | No durable reservation; retry may execute | `test_rollback_before_commit_allows_retry` |
| G — Business rejection | Reservation rolls back; retry may reevaluate | `test_business_rejection_does_not_retain_reservation` |

**Business rejection policy (documented + tested):** insufficient funds does **not** durably store a negative decision under the key. After funding, the same key succeeds with 201. This is intentional for the laboratory and must not be silently changed.

---

## 4. Database guarantees

### 4.1 Migration 003 schema (applied)

From `alembic/versions/003_create_idempotency_records.py` and live `pg_indexes` / `pg_constraint`:

**Table `idempotency_records`**

| Column | Notes |
| --- | --- |
| `id` | UUID PK |
| `scope` | `varchar(64)` NOT NULL |
| `idempotency_key` | `varchar(128)` NOT NULL |
| `request_fingerprint` | `varchar(64)` NOT NULL |
| `transfer_id` | UUID NULL FK → `transfers(id)` |
| `status` | `reserved` \| `completed` |
| `created_at` / `completed_at` | timestamptz; completed_at NULL iff reserved |

**Constraints / indexes (observed):**

- `UNIQUE (scope, idempotency_key)` — `uq_idempotency_scope_key`
- Partial unique index on `transfer_id` WHERE NOT NULL
- Check `ck_idempotency_status_shape`: reserved ⇒ `transfer_id` and `completed_at` NULL; completed ⇒ both set

### 4.2 `execute_idempotent_transfer` (source)

```35:73:app/ledger/idempotency.py
def execute_idempotent_transfer(
    session: Session,
    *,
    idempotency_key: str,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    currency: str,
) -> IdempotentTransferResult:
    key = validate_idempotency_key(idempotency_key)
    fingerprint = transfer_fingerprint(...)
    maybe_fault("before_idempotency_reservation")
    reserved = _try_reserve(session, key=key, fingerprint=fingerprint)
    if reserved is None:
        return _replay_or_conflict(session, key=key, fingerprint=fingerprint)
    ...
    transfer = execute_transfer(... after_debit=...)
    reserved.transfer_id = transfer.id
    reserved.status = "completed"
    reserved.completed_at = datetime.now(UTC)
    session.flush()
    maybe_fault("before_commit")
    return IdempotentTransferResult(transfer=transfer, replay=False)
```

Reservation uses a **savepoint** (`begin_nested`) so a unique violation does not poison the outer session:

```76:89:app/ledger/idempotency.py
def _try_reserve(...):
    ...
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        return None
    return record
```

### 4.3 End-to-end transaction trace (protected HTTP)

1. Validate body (Pydantic) and key format.
2. `execute_idempotent_transfer` in the request session.
3. `INSERT` reserved row (unique index serializes concurrent same-key inserts: waiter blocks until winner commits/aborts).
4. On conflict: `SELECT … FOR UPDATE` + fingerprint compare → replay or `IdempotencyKeyConflict`.
5. On win: `execute_transfer` locks accounts (sorted IDs), relative debit/credit, two ledger rows.
6. Mark record `completed` with `transfer_id`.
7. API `session.commit()` — **one atomic commit** of reservation completion + transfer + ledger.
8. Optional test fault `after_commit_before_response`.
9. Return 201/200 JSON.

**Atomicity proof (source-code guarantee):** There is no commit between reservation and transfer completion. A reserved-only row is never committed by this path. Crash/abort before commit removes both financial changes and the uncommitted reservation (tested in rollback cases).

**PostgreSQL unique-index behavior (architectural fact + design intent):** Concurrent second `INSERT` of the same `(scope, key)` waits for the first transaction; after first commits completed, second gets unique violation → savepoint abort → replay path. After first aborts, second’s insert can succeed.

**Permanently blocking processing record?** Application path does **not** commit a durable `processing`/`reserved` row alone. Schema *allows* reserved shape, but the service only commits after `completed`. Remaining risk is a **manual or alternate writer** committing reserved rows — not observed in this codebase’s public path.

### 4.4 Lock order

Documented in `idempotency.py` header:

1. Idempotency unique insert  
2. Account `FOR UPDATE` in UUID order  
3. Ledger inserts / complete record  

This avoids holding account locks while waiting on an unresolved same-key insert (losers block on the unique index **before** calling `execute_transfer`).

---

## 5. Concurrency and failure timelines (test-backed)

### 5.1 Sequential duplicate (HTTP)

**Test:** `tests/test_idempotency.py::test_sequential_duplicate_http`

| Metric | Observed |
| --- | --- |
| HTTP requests | 2 |
| Logical operations | 1 |
| Transfers | 1 |
| Transfer ledger entries | 2 |
| Idempotency records | 1 |
| Source/dest balances (start 2000) | 1500 / 500 |
| Status codes | 201 then 200 |
| Invariants | 0 violations |

### 5.2 Eight concurrent same-key requests (service)

**Test:** `test_concurrent_duplicate_same_key` — 8 threads, barrier, `execute_idempotent_transfer` (not HTTP).

| Metric | Observed |
| --- | --- |
| Attempts | 8 |
| Distinct transfer IDs | 1 |
| Transfers | 1 |
| Balances | 1500 / 500 |
| Invariants | 0 |

**Benchmark corroboration:** `storm-safe.json` — 8 attempts, 1 transfer, 7 deduplicated, 0 duplicates.

### 5.3 Commit then lost response (HTTP)

**Test:** `test_lost_response_after_commit_http`

Timeline:

1. Fault hook on `after_commit_before_response` raises `RuntimeError("lost")` **after** `session.commit()`.
2. First `TestClient(..., raise_server_exceptions=False)` returns **500** (no success body).
3. DB already has **1** transfer; invariants 0.
4. Retry same key/payload → **200**, `idempotent_replay=true`, still **1** transfer, balances 1500/500.

**Classification:** Exercises FastAPI route + commit ordering via in-process ASGI. It is **not** a kernel-level TCP disconnect. See §6.

### 5.4 Rollback before commit, then retry

**Test:** `test_rollback_before_commit_allows_retry` (service + `before_commit` fault)

| After abort | After retry |
| --- | --- |
| transfers 0, idempotency_records 0 | 1 transfer, balances 1500/500, `replay=False` |

### 5.5 Same key, different payload

**Test:** `test_payload_conflict_http`

| After first 201 | After conflicts |
| --- | --- |
| 1 transfer, dest1=500 | Still 1 transfer; dest2=0; both conflict responses 409 |

### 5.6 Concurrent different keys / shared source

**Test:** `test_concurrent_different_keys_respect_balance` — two keys, amount 800, funding 1000 → exactly one success (`ok`/`poor`), one transfer. Idempotency does **not** bypass balance protection.

---

## 6. Unsafe baselines

### 6.1 Implementations (`experiments/experiment_003/unsafe.py`)

| Strategy | Mechanism | Why vulnerable |
| --- | --- | --- |
| `retry_without_idempotency` | Always `execute_transfer` | Lost-response retry creates a second movement |
| `naive_check_then_insert` | `SELECT` then transfer+`INSERT` into `unsafe_idempotency_attempts` **without** unique `(scope,key)` | Concurrent misses both create transfers |

Production `idempotency_records` unique index is **not** weakened.

### 6.2 Duplicate-effect definition (benchmark)

From `experiments/experiment_003/benchmark.py`:

| Strategy / scenario | Duplicate count |
| --- | --- |
| `safe` | `max(0, count(transfers) - count(idempotency_records))` |
| `concurrent_duplicate_storm` (non-safe) | `max(0, transfers - 1)` |
| Sequential / failure `unsafe_retry` | Incremented when a retry attempt also `created` |

**Warmup note:** Traffic scenarios create warmup transfers (2) + measured (40) ⇒ `committed_transfers` often **42**. Safe duplicate formula uses record count so warmup does not appear as duplicates.

### 6.3 Reported numbers vs JSON

| Claim | JSON file | Observed |
| --- | --- | --- |
| 40 duplicate effects sequential unsafe | `sequential-unsafe.json` | `duplicate_transfer_effects: 40`, `committed_transfers: 84`, `service_attempts: 80` |
| Eight transfers naive storm | `storm-naive.json` | `committed_transfers: 8`, `duplicate_transfer_effects: 7` |
| One transfer protected storm | `storm-safe.json` | `committed_transfers: 1`, `duplicate_transfer_effects: 0`, `deduplicated_requests: 7` |
| Zero duplicates protected cells | baseline/sequential/mixed/failure/storm safe | All `duplicate_transfer_effects: 0` |

### 6.4 Deterministic naive barrier

Naive storm/tests pass `after_missed_lookup` → second `Barrier` after SELECT miss and **before** transfer. That forces every worker to observe absence before any insert commits.

**Protected storm** uses only a start barrier, then unique-index wait — **different interleaving and latency profile**. Do **not** treat storm p50 (safe ~20.8 ms vs naive ~36.8 ms) as an apples-to-apples “idempotency tax”; naive deliberately widens the race window and then serializes on account locks for eight full transfers.

---

## 7. Lost-response recovery (critical)

### 7.1 HTTP test guarantees

| Requirement | Verified? | How |
| --- | --- | --- |
| Commit before failure | **Yes** | `session.commit()` then `maybe_fault("after_commit_before_response")` in `api.py` 179–180 |
| Client did not get success | **Yes** | status 500; no 201 body |
| Retry same key/payload | **Yes** | same headers/body |
| Retry returns committed transfer | **Yes** | 200 + `idempotent_replay` |
| No second movement | **Yes** | still one transfer; balances 1500/500 |

### 7.2 Benchmark 20/20

`failure-safe.json`:

- `logical_operations: 20`
- `service_attempts: 40`
- `committed_transfers: 20`
- `recovered_lost_responses: 20`
- `deduplicated_requests: 20`
- `duplicate_transfer_effects: 0`
- `reliability.error_counts: {"error:RuntimeError": 20}`
- `transport_timeouts: 20` (label used for injected post-commit errors)

Mechanism: `_attempt_safe` commits via `session.begin()`, then calls `maybe_fault("after_commit_before_response")` — service-level mirror of the API ordering, **not** HTTP.

### 7.3 Simulated vs real network loss

| Aspect | This experiment |
| --- | --- |
| Failure mode | In-process exception after commit |
| Transport | ASGI `TestClient` / direct Python call |
| Real TCP reset / LB timeout / partial HTTP | **Not verified** |

The experiment **does** prove: durable commit can exist without a successful client-visible response, and same-key retry can recover without a second debit. It does **not** prove every real-world disconnect class.

---

## 8. Financial integrity

### 8.1 Still enforced

| Invariant | Enforced by | Exp 003 interaction |
| --- | --- | --- |
| Non-negative customer balance | SQL check + conditional UPDATE | Unchanged `execute_transfer` |
| Atomic debit/credit + ledger pair | Single DB transaction | Same transaction as idempotency completion |
| Reconciliation detectors | `reconcile.violations` | Asserted in Exp 003 tests + benchmark JSON |
| Concurrent overspend | Account locks + debit predicate | `test_concurrent_different_keys_respect_balance` |
| Stale ORM | `populate_existing` | `test_idempotent_transfer_with_previously_loaded_accounts` |
| Currency match | Application raise → 409 | Still mapped |

### 8.2 Bypass analysis

- Public HTTP transfers only call `execute_idempotent_transfer`.
- `execute_transfer` remains callable from tests/benchmarks/unsafe strategies (laboratory). That is intentional for Exp 001/002/003 baselines, **not** a public unkeyed API.
- Idempotency replay returns an existing transfer; it does not re-run debit/credit.

### 8.3 Balanced ledger ≠ exactly-once intent

Unsafe sequential retries: **84** transfers, **0** invariant violations, **40** duplicate effects. **Observed:** each transfer can reconcile while business intent (40 logical ops) was applied twice.

---

## 9. Benchmark methodology

### 9.1 Strategies and scenarios

| Strategy | Code path |
| --- | --- |
| `safe` | `execute_idempotent_transfer` |
| `unsafe_retry` | `retry_without_idempotency` |
| `naive_check` | `naive_check_then_insert` (+ barrier in storm) |

Scenarios: `baseline`, `sequential_retries`, `concurrent_duplicate_storm`, `mixed_traffic`, `failure_recovery`.

### 9.2 Recorded matrix parameters

| Cell | workers | warmup | iterations | duplicate_rate | notes |
| --- | --- | --- | --- | ---: | --- |
| baseline-safe / raw | 1 | 2 | 40 | 0.3 (unused in baseline) | unique keys |
| sequential-* | 1 | 2 | 40 | 1.0 | every measured op retried |
| mixed-safe | 1 | 2 | 40 | 0.3 | seeded RNG 42 |
| storm-* | 8 | 0 | 1 | n/a | one shared key |
| failure-* | 1 | 0 | 20 | n/a | post-commit inject on safe |

Common: `amount_minor=25`, `seed=42`, `retry_budget=3`, backoff base 1 ms / cap 50 ms, full jitter. **Repetitions:** one recorded pass per file (no multi-run statistics).

### 9.3 Measurement details

| Topic | Behavior |
| --- | --- |
| Latency samples | All measured `request_latencies` (includes failed injected attempts when classified) |
| Percentiles | Inclusive nearest-rank via Exp 001 `_order_statistic_ms` |
| DB init | Truncate financial tables; drop/recreate unsafe table as needed |
| Pool | `pool_size=max(8, workers)` |
| Exit codes | Violations → 2; safe+duplicates → 3 |
| Deadlocks / lock timeouts / SQLSTATE | **Not separately instrumented** in Exp 003 JSON (`reliability.error_counts` only) |
| HTTP throughput | **N/A** — service attempts only |

### 9.4 Throughput meanings

| Metric | Meaning |
| --- | --- |
| `requests_per_second` | Measured service attempts / sum(attempt latencies) |
| `unique_transfers_per_second` | `committed_transfers / same elapsed` — **includes duplicate transfers** for unsafe; includes warmup for traffic cells |
| Logical ops | Intent count (`logical_operations` / `unique_logical_successes`) |

**Do not** read unsafe `unique_transfers_per_second` as unique business completions.

---

## 10. Raw benchmark results

Deadlocks, lock timeouts, SQLSTATE histograms: **N/A** (not recorded). Conflicts: 0 in all files. Invariant violations: 0 in all files.

### 10.1 Unique-key baseline

| File | Strategy | Logical ops | Attempts | Transfers | Dups | Dedup | RPS | p50 | p95 | p99 | Logical p50 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `benchmark-results/experiment_003/baseline-raw.json` | unsafe_retry | 40 | 40 | 42 | 0 | 0 | 322.875 | 3.023 | 3.555 | 3.609 | 3.427 |
| `benchmark-results/experiment_003/baseline-safe.json` | safe | 40 | 40 | 42 | 0 | 0 | 230.967 | 4.304 | 5.225 | 5.454 | 4.307 |

### 10.2 Sequential retries

| File | Strategy | Logical | Attempts | Transfers | Dups | Dedup | Retries | RPS | p50 | p95 | p99 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `sequential-safe.json` | safe | 40 | 80 | 42 | 0 | 40 | 40 | 288.137 | 3.561 | 5.332 | 5.971 |
| `sequential-unsafe.json` | unsafe_retry | 40 | 80 | 84 | 40 | 0 | 40 | 355.930 | 2.826 | 3.180 | 4.137 |

### 10.3 Concurrent duplicate storm

| File | Strategy | Attempts | Transfers | Dups | Dedup | RPS | p50 | p95 | p99 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `storm-safe.json` | safe | 8 | 1 | 0 | 7 | 48.564 | 20.770 | 23.228 | 23.228 |
| `storm-naive.json` | naive_check | 8 | 8 | 7 | 0 | 25.444 | 36.798 | 48.975 | 48.975 |

### 10.4 Mixed traffic

| File | Strategy | Logical | Attempts | Transfers | Dups | Dedup | Retries | RPS | p50 | p95 | p99 | Logical p50 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `mixed-safe.json` | safe | 40 | 53 | 42 | 0 | 13 | 13 | 248.263 | 4.344 | 5.237 | 5.878 | 4.604 |

### 10.5 Failure recovery

| File | Strategy | Logical | Attempts | Transfers | Dups | Recovered | Transport timeouts | RPS | p50 | p95 | p99 | Retry p50 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `failure-safe.json` | safe | 20 | 40 | 20 | 0 | 20 | 20 | 262.362 | 3.485 | 5.793 | 6.028 | 2.499 |
| `failure-unsafe.json` | unsafe_retry | 20 | 40 | 40 | 20 | 0 | 20 | 321.046 | 2.928 | 3.800 | 4.020 | N/A |

`failure-unsafe` marks `transport_timeouts: 20` without injecting a real exception on the first attempt — it **simulates** “lost response” by unconditionally performing a second transfer. Treat as experimental labeling, not identical fault machinery to `failure-safe`.

---

## 11. Idempotency overhead audit

### 11.1 Reported comparison (baseline unique keys)

| | Raw (`unsafe_retry`) | Safe |
| --- | ---: | ---: |
| RPS | 322.875 | 230.967 |
| p50 ms | 3.023 | 4.304 |
| p95 ms | 3.555 | 5.225 |
| p99 ms | 3.609 | 5.454 |

**Observed deltas:** RPS −28.5%; p50 +1.281 ms (+42.4%).

### 11.2 Are workloads equivalent?

**Mostly yes for the financial core**, with caveats:

| Factor | Raw | Safe |
| --- | --- | --- |
| Accounts / amount / workers / iterations | Same | Same |
| `execute_transfer` | Yes | Yes |
| Idempotency validate + fingerprint + INSERT + possible unique coordination | No | Yes |
| Key header / HTTP | No (service) | No (service) |
| Extra ORM flush for completed record | No | Yes |

**Causal claim supported at laboratory strength:** protected unique-key path is slower on this machine under this harness.  
**Not supported:** attributing 100% of the gap solely to “the unique index” without isolating statement timings (no `EXPLAIN ANALYZE` / `pg_stat_statements` capture in the JSON).

### 11.3 Contention tails

Under eight-way same-key storm, safe p50≈20.8 ms and p99≈23.2 ms — far above unique-key baseline p99≈5.5 ms. **Observed:** high duplicate contention on one key increases latency via unique-index wait + single winner’s transfer. Naive storm is slower still because it commits eight transfers after a forced race barrier.

---

## 12. Automated tests and isolation

### 12.1 Coverage matrix (Exp 003)

| Test | File | Asserts |
| --- | --- | --- |
| Lab URL refuse | `test_idempotency.py` | non-`experiments` DB rejected |
| Fingerprint/key | same | stable digest; short key rejected |
| Sequential HTTP | same | 201/200; 1 transfer; 2 ledger rows; balances |
| Concurrent same key | same | 8→1 transfer |
| Payload conflict | same | 409; no extra movement |
| Lost response HTTP | same | 500 then 200 replay; 1 transfer |
| Rollback before commit | same | empty tables; retry creates |
| Rollback after debit | same | reservation cleared |
| Different keys / balance | same | one of two succeeds |
| Stale ORM | same | balances after preloaded account |
| Business rejection | same | no record; later success |
| Service conflict | same | `IdempotencyKeyConflict` |
| Isolation marker | same | starts empty |
| Unsafe retry dup | `test_experiment_003_unsafe.py` | 2 transfers |
| Naive barrier race | same | `transfer_count > 1` |

Exp 001/002 regression: included in full 43-test suite (ledger + counters + phase2 + retry unit tests).

### 12.2 Isolation guard

`app/laboratory.py` allows hosts `{localhost,127.0.0.1,db,::1}` and DB name `experiments` only. Independently verified: `production` DB name refused; laboratory URL accepted. Fixtures truncate **before and after** each `client` / `laboratory_database` test and clear fault hooks.

### 12.3 Independent suite results

| Run | Result |
| --- | --- |
| Ruff / Mypy | Pass |
| Pytest #1 / #2 | 43 passed each |
| `pytest -m postgres` | **36 passed**, 0 skipped among selected |

---

## 13. Adversarial review

| # | Question | Finding | Severity | Evidence | Uncertainty |
| --- | --- | --- | --- | --- | --- |
| 1 | Two concurrent same key → two transfers? | **Not observed** on protected path | High if broken | Concurrent test + storm-safe | Extreme timing / alternate isolation **Not verified** at SERIALIZABLE |
| 2 | Key commit without transfer? | Public path marks completed before commit; reserved-only commit not performed | Medium | Code + shape check | Manual SQL inserting reserved **possible** (schema allows) |
| 3 | Transfer without key? | HTTP requires key; `execute_transfer` still exists off-API | Low (lab) | `api.py` / `service.py` | Operators calling service directly |
| 4 | Conflicting payload reuse? | Rejected 409 | High if broken | Tests + live probe | — |
| 5 | Crash leaves key permanently blocked? | No durable processing commit in path | Medium | Design + rollback tests | Process kill exactly between statements still rolls back uncommitted txn |
| 6 | Session unusable after unique conflict? | Savepoint catches `IntegrityError` | Medium | `_try_reserve` | Other error classes less exercised |
| 7 | New deadlock from lock order? | Same-key waits on unique before accounts; accounts still ordered | Medium | Design | Multi-resource workflows beyond two accounts **Not verified** |
| 8 | Lost response → second movement? | Protected: no; unsafe: yes | High | HTTP test; failure JSON | Real network partitions |
| 9 | Business rejection replayed incorrectly? | Rejection not stored; retry reevaluates | Medium | Explicit test | Product may want sticky declines — **policy choice** |
| 10 | Malformed / long key? | 400 via regex 8–128 | Low | Validation + API | Header size limits at proxies **Not verified** |
| 11 | Lab-wide key scope collisions? | **Yes, by design** — no tenancy | High for multi-tenant misuse | ADR / fingerprint docs | — |
| 12 | Dedup reveals another client’s op? | Replay returns transfer for that key; any client who can guess/reuse key learns result | High in shared lab | Contract | Auth not implemented |
| 13 | Zero dups while other invariant broken? | Benchmark exits on `violations`; also checks dups for safe | Medium | `main()` exit codes | Duplicate formula is record-count heuristic, not per-key ledger proof |
| 14 | p99 excludes failures? | **No** — all measured attempt latencies included | Medium | `_Window.request_latencies` | Logical e2e percentiles separately tracked when recorded |

---

## 14. Publication claims

| Claim | Supported? | Cite |
| --- | --- | --- |
| Retries without idempotency can duplicate financial effects | **Yes** | `sequential-unsafe.json` (40 dups); unsafe unit test |
| Balanced ledger ≠ exactly-once business intent | **Yes** | Unsafe cells with `invariant_violations: 0` and dups > 0 |
| PostgreSQL uniqueness can coordinate concurrent same-key requests | **Yes** | Unique constraint + concurrent tests + storm-safe |
| Protected path ≤ one transfer per scope/key/equivalent payload | **Yes (lab evidence)** | Tests + all safe JSON cells |
| Request can commit while HTTP success is lost | **Yes (simulated)** | `test_lost_response_after_commit_http` |
| Same-key retry recovers original result | **Yes** | Same test; `failure-safe.json` 20/20 |
| Different payload same key rejected | **Yes** | Conflict tests + live 409 |
| Idempotency adds measurable overhead | **Yes, with qualifications** | baseline-raw vs baseline-safe (§11) |
| High duplicate contention affects latency | **Yes** | storm-safe vs baseline-safe p50/p99 |
| Results prove universal distributed exactly-once | **No — correctly not claimed** | Single DB, no brokers, simulated loss |

---

## 15. Final assessment

### Confirmed findings

1. Protected `POST /transfers` requires `Idempotency-Key` and commits idempotency completion with the transfer atomically.
2. Sequential and concurrent equivalent retries produce one transfer; conflicts reject mismatched payloads.
3. Simulated post-commit response loss + same-key retry recovers without a second movement (HTTP test + 20/20 benchmark).
4. Unsafe retries and barrier-forced naive check-then-insert duplicate movements while remaining ledger-reconciled per transfer.
5. Full quality suite passes twice; 36 PostgreSQL tests execute; laboratory truncate is URL-guarded.
6. Unique-key protected path is slower than raw `execute_transfer` under this harness (~231 vs ~323 RPS).

### Remaining risks

1. Uncommitted working tree — no immutable commit pin for Exp 003 code.
2. Laboratory-wide key scope / no authentication — key reuse across “clients” is trivial.
3. Business rejections are not sticky under the key.
4. Schema permits a reserved row shape that the app does not commit; alternate writers could abuse it.
5. Benchmarks are single-pass, service-level, small N; storm naive uses an artificial barrier.
6. `unique_transfers_per_second` naming can mislead when duplicates exist.
7. Real network disconnect classes not exercised.

### Unverified claims

- Production multi-region / multi-primary exactly-once.
- Behavior under `REPEATABLE READ` / `SERIALIZABLE` for idempotency waits.
- Precise DB-time attribution of the ~1.3 ms p50 overhead.
- Docker/host memory limits during this audit session.
- HTTP load-generator throughput (only `TestClient` correctness).

### Methodological limitations

- Dirty-tree evidence set.
- In-process fault injection ≠ packet loss.
- One recorded benchmark repetition per cell.
- Safe duplicate detector uses `transfers - idempotency_records`, not a cryptographic audit of each key.

### Recommended corrections (do not implement in this audit)

1. Commit Experiment 003 so publication cites a clean SHA.
2. Rename or split throughput metrics (attempts/s vs unique successful business ops/s vs committed transfer rows/s).
3. Record SQLSTATE/deadlock counters explicitly (even if zero).
4. Add an authenticated or tenant-scoped key when leaving laboratory posture.
5. Document sticky vs reevaluating business declines as a product decision.
6. Optionally forbid committing `reserved` via a deferred constraint / reject trigger if defense-in-depth is desired.

### Publication readiness

## READY WITH QUALIFICATIONS

**Justification:** Correctness evidence for the laboratory idempotency contract is strong (schema uniqueness, atomic service path, deterministic tests, protected benchmark cells with zero duplicate effects and zero invariant violations, unsafe baselines showing the failure mode). Qualifications are mandatory in any public write-up: fictional money, single PostgreSQL laboratory, simulated lost responses, uncommitted tree at audit time, no tenancy, and performance numbers that are environment-specific comparisons—not capacity ratings.

**Do not declare this implementation safe for real financial production use.**

---

*End of independent technical evidence report — Experiment 003.*
