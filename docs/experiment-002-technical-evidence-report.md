# Experiment 002 — Independent Technical Evidence Report

**Document type:** Independent technical evidence report  
**Subject:** Experiment 002 — Building a Reliable Transaction API with Python  
**Repository:** `engineering-beyond-assumptions` (`https://github.com/renandeocleciano/engineering-beyond-assumptions`)  
**Local path:** `/Users/renandeocleciano/Projetos/engineering-beyond-assumptions`  
**Report date:** 2026-11-25  
**Author role:** Independent Principal Backend Engineer / Technical Auditor  

This report is intended for a senior engineer without repository access. Every claim is classified as observed measurement, source-code guarantee, automated-test evidence, architectural assumption, known limitation, or **Not verified**.

**No application code, migrations, tests, or benchmark implementations were modified while producing this report.** Read-only verification commands were executed. Laboratory financial tables were truncated once after an independent pytest failure so that a second clean suite run could be recorded; that truncation did not change repository files.

---

## 1. Executive summary

Experiment 002 adds a fictional-money transaction API (accounts, laboratory funding, synchronous transfers, signed ledger) beside Experiment 001’s counter study. An audit-identified SQLAlchemy identity-map race in concurrent funding was **reproduced** (stored balance 600 vs ledger-derived 700 after two concurrent +100 funds on a 500 starting balance), then corrected with `populate_existing` locks and relative SQL balance updates. Corrected benchmarks added `pessimistic_service` (calls `execute_transfer` in a fresh session) alongside `pessimistic_sql`.

**Independent quality re-run (this report):** Ruff passed; mypy passed (27 files); Alembic `002 (head)`; pytest **28 passed** after an explicit `TRUNCATE` of laboratory tables. An earlier suite run in the same session reported **1 failed, 27 passed** on `test_account_fund_transfer_and_entries` (`violations == 0`); the root cause of that single failure was **not reconstructed** because fixture teardown emptied the tables before diagnosis.

**Git state:** Experiment 002 artifacts are **uncommitted**. `HEAD` is `7633b672c87a30de708231fb9bc976eca231d7ac` (“Experimental 1”, 2026-11-23). There is **no git commit SHA that uniquely identifies the Experiment 002 implementation**. Reviewers must treat the working tree (and this report’s file/line citations) as the evidence set.

---

## 2. Repository and environment

### 2.1 Relevant structure

| Path | Role |
| --- | --- |
| `app/main.py` | FastAPI app; includes ledger router; counter routes; `/health` |
| `app/db.py` | Engine, `SessionLocal`, `get_session` (no auto-commit) |
| `app/ledger/` | Domain package: `api.py`, `service.py`, `models.py`, `reconcile.py`, `errors.py` |
| `alembic/versions/002_create_ledger.py` | Schema revision 002 |
| `experiments/experiment_002/strategies.py` | Naive / conditional / pessimistic SQL / optimistic |
| `experiments/experiment_002/benchmark.py` | Workloads + `pessimistic_service` |
| `experiments/experiment_002/README.md` | Protocol and recorded results |
| `tests/test_ledger.py` | PostgreSQL integration tests |
| `docs/adr/0003-transfer-locking-and-ledger.md` | ADR |
| `benchmark-results/experiment_002/` | 24 historical JSON cells (`phase: experiment-002`) |
| `benchmark-results/experiment_002_corrected/` | 51 corrected JSON cells (`phase: experiment-002-corrected`) |
| `compose.yaml` | Postgres 17 + app |
| `.github/workflows/ci.yml` | CI with Postgres 17 on 5432 |

Experiment 001 paths (`experiments/experiment_001/`, counter models, revision 001) remain present. `git status` does not list Experiment 001 modules as modified.

### 2.2 Versions (measured on this host)

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
| `lock_timeout` (server default) | `0` (disabled) |
| `max_connections` | 100 |
| `shared_buffers` | 128MB |
| `fsync` | on |
| `synchronous_commit` | on |
| Host OS | Darwin 25.0.0 arm64 (Apple M-series reported in prior runs as M5) |
| Host CPUs / RAM | `sysctl hw.ncpu=10`, `hw.memsize=17179869184` (~16 GiB) |
| Docker | Server 28.4.0; `NCPU=2`; `MemTotal=2054565888` (~1.91 GiB) |

### 2.3 Application vs benchmark environments

| Concern | Application (Compose `app`) | Benchmark / pytest (host) |
| --- | --- | --- |
| Process | Uvicorn in container | Host `.venv` Python |
| DB URL | `...@db:5432/experiments` | `...@localhost:5434/experiments` |
| Port publish | Host `8000` → app; host `5434` → Postgres | Same Postgres via 5434 |
| During matrix | App container **stopped** | Workers open DB connections directly |
| HTTP | Served | **Not** measured by `pessimistic_service` |

### 2.4 Benchmark windows

| Matrix | Directory | Cells | Timestamp span (UTC) |
| --- | --- | --- | --- |
| Historical | `benchmark-results/experiment_002/` | 24 | 2026-11-25T22:10:24.018427+00:00 → 2026-11-25T22:23:28.611136+00:00 |
| Corrected | `benchmark-results/experiment_002_corrected/` | 51 | 2026-11-25T22:46:00.041087+00:00 → 2026-11-25T22:57:08.214348+00:00 |

### 2.5 Reproduction commands

```sh
docker compose up -d db
export DATABASE_URL='postgresql+psycopg://experiments:experiments@localhost:5434/experiments'
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
ruff check .
mypy
# Prefer an empty laboratory schema before pytest (fixture truncates after each test, not before):
# TRUNCATE ledger_entries, fundings, transfers, accounts;
pytest
docker compose stop app
python -m experiments.experiment_002.benchmark --scenario hot-source --strategy pessimistic_service
python -m experiments.experiment_002.benchmark --scenario opposite --strategy pessimistic_service
```

CI uses `DATABASE_URL` on port **5432** inside the runner (`.github/workflows/ci.yml`).

---

## 3. Architecture and data model

### 3.1 Mermaid architecture

```mermaid
flowchart TB
  Client[HTTP Client / TestClient]
  Bench[experiment_002.benchmark]
  Strat[strategies.py SQL twins]
  API[app.ledger.api router]
  Svc[app.ledger.service]
  Rec[app.ledger.reconcile]
  Sess[SQLAlchemy Session / Connection]
  PG[(PostgreSQL 17<br/>accounts fundings transfers ledger_entries<br/>+ counters)]

  Client --> API
  API --> Svc
  API --> Sess
  Svc --> Sess
  Bench --> Strat
  Bench --> Svc
  Bench --> Rec
  Strat --> Sess
  Svc --> Sess
  Sess --> PG
  Rec --> PG
```

### 3.2 Session lifecycle

`app/db.py` creates a process-wide engine (`pool_pre_ping=True`) and `SessionLocal`. `get_session` yields a session and does **not** commit. Routes call `session.commit()` after successful service work (`app/ledger/api.py` lines 112–170). Unexpected exceptions leave the session to close without commit (rollback on close).

### 3.3 PostgreSQL schema (live `\d+` + migration 002)

#### `accounts`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | |
| `kind` | varchar(16) | CHECK `customer` \| `treasury` |
| `currency` | varchar(3) | CHECK length 3 |
| `balance_minor` | bigint | CHECK `>= 0 OR kind = treasury` |
| `status` | varchar(16) | CHECK `open` \| `closed` |
| `version` | bigint | optimistic strategy |
| `created_at` / `updated_at` | timestamptz | |

Index: partial unique `uq_accounts_treasury_currency` on `currency` WHERE `kind = 'treasury'`.

#### `fundings`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | |
| `account_id` | uuid FK → accounts | customer |
| `treasury_account_id` | uuid FK → accounts | |
| `amount_minor` | bigint | CHECK `> 0` |
| `currency` | varchar(3) | |
| `created_at` | timestamptz | |

#### `transfers`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | |
| `source_account_id` / `destination_account_id` | uuid FK | CHECK distinct |
| `amount_minor` | bigint | CHECK `> 0` |
| `currency` | varchar(3) | |
| `status` | varchar(16) | CHECK only `'completed'` |
| `created_at` / `completed_at` | timestamptz | |

#### `ledger_entries`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | |
| `account_id` | uuid FK | |
| `transfer_id` / `funding_id` | uuid FK nullable | CHECK exactly one set |
| `amount_minor` | bigint | CHECK `<> 0` |
| `currency` | varchar(3) | |
| `created_at` | timestamptz | |

Index: `ix_ledger_entries_account_created` (`account_id`, `created_at`, `id`).  
Trigger: `ledger_entries_no_mutation` BEFORE UPDATE OR DELETE → function raises `ledger entries are immutable`.

### 3.4 What PostgreSQL enforces vs application / reconciliation

| Concern | PostgreSQL | Application | Reconciliation only |
| --- | --- | --- | --- |
| Non-negative customer balance | CHECK | Relative debit predicate | Detects projection drift |
| Positive transfer/funding amount | CHECK | Service validation | — |
| Non-zero ledger amount | CHECK | — | — |
| Exactly one of transfer/funding on entry | CHECK | `_post` args | — |
| Two balancing entries per movement | **No** | Service posts pair | Detects |
| Entries name the movement’s accounts | **No** | Service | Detects |
| Currency consistency | **No** | Service match check | Detects |
| Balance = sum(entries) | **No** | Relative update intent | Detects |
| Immutability of entries | Trigger on UPDATE/DELETE | Never updates/deletes | — |
| `TRUNCATE` protection | **No** (row triggers do not fire) | Tests use TRUNCATE | — |

**Do not claim** that CHECK constraints enforce cross-row double-entry correctness. They do not.

### 3.5 Balance semantics

`accounts.balance_minor` is a **stored projection**, not a derived view. Ledger entries are the movement journal. Reconciliation compares projection to `SUM(ledger_entries.amount_minor)` per account. A balanced transfer pair can coexist with a wrong projection (demonstrated by naive strategy and the original funding race).

---

## 4. Financial invariants matrix

| # | Invariant | Enforcement | Code | Test | Validation evidence | Limitations |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Customer balance ≥ 0 | DB CHECK + SQL debit predicate | `002_create_ledger.py` L30; `service.py` `_apply_delta` L193–196 | overspend, insufficient funds | Overspend test expects exactly one success | CHECK alone does not stop stale absolute writes that stay non-negative |
| 2 | Debit only if funds available | Early ORM check + conditional UPDATE | `execute_transfer` L92–93; `_apply_delta` | `test_concurrent_overspend…`, `test_rejects…`, `test_loaded_balance_cannot_spend…` | Observed in tests | Early check can be stale; predicate is authoritative |
| 3 | Debit+credit atomic | Single DB transaction | API commit after service; `session.begin()` in tests | `test_failure_after_debit_rolls_back` | Rollback leaves balances unchanged, 0 transfers | Client timeout after commit **Not verified** by test |
| 4 | Ledger pair sums to 0 | Application posts ±amount | `_post` / strategies `_insert_movement` | happy path; planted unbalanced | Planted uneven transfer detected | No DB constraint |
| 5 | Correct ledger accounts | Application | `_post` account_id; reconcile `_TRANSFER_ACCOUNTS` | planted mismatch | Detected | No DB constraint |
| 6 | Matching currencies | Service raises `CurrencyMismatch` | `execute_transfer` L90–91 | `test_rejects…` 409 | Observed | Strategy C does not check |
| 7 | Projection = ledger sum | Relative UPDATE + reconcile | `_apply_delta`; `invariant_report` | concurrent funding; planted drift | Corrected funding test expects 700/700 | Naive strategy still violates in benchmarks |
| 8 | Funding balanced via treasury | App posts −treasury / +customer | `fund_account` L52–53 | funding tests | Observed | Treasury may be negative by CHECK design |
| 9 | No partial effect after failure | Transaction abort | `after_debit` hook | `test_failure_after_debit_rolls_back` | Observed | Hook is test-only |
| 10 | Concurrent overspend safety | Ordered locks + conditional debit | service | `test_concurrent_overspend…` | Exactly one of two 800-from-1000 | Two workers only |
| 11 | HTTP 201 only after commit | Route order | `api.py` L146–170 | HTTP smoke in prior session | Prior session: 201 then balances | Not re-run in this evidence pass as full matrix |
| 12 | Rejected ops leave no transfer | No insert / rollback | service + API | insufficient funds asserts transfer count 0 | Observed | Declined attempts not stored |

---

## 5. Original funding race — before the fix

### 5.1 Availability of original source in git

**Not verified in git history.** Experiment 002 was never committed. The pre-fix `_lock_in_id_order` / `_post` bodies are recovered from the agent session archive (compaction transcript), which matches the audit description.

### 5.2 Original code (reconstructed from session archive)

```python
def _lock_in_id_order(session: Session, first: UUID, second: UUID) -> dict[UUID, Account]:
    locked: dict[UUID, Account] = {}
    for account_id in sorted((first, second)):
        account = session.scalar(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        if account is not None:
            locked[account.id] = account
    return locked


def _post(...):
    session.add(LedgerEntry(...))
    account.balance_minor += amount_minor
    account.version += 1
    ...
    session.flush()
```

`fund_account` called `_require_customer` (identity-map load) **before** `_lock_in_id_order`. The lock query did **not** pass `populate_existing=True`.

### 5.3 Deterministic reproduction (test design)

`tests/test_ledger.py` `test_concurrent_funding_uses_the_balance_loaded_before_the_lock` (lines 249–280):

1. Create account; fund to **500**.
2. Two sessions each `session.get(Account)` and assert `balance_minor == 500`.
3. Barriers `loaded` then `start` — **before** `fund_account` / before `SELECT FOR UPDATE`.
4. Each calls `fund_account(..., 100)`.

### 5.4 Observed pre-fix outcome

| Observation | Value | Evidence class |
| --- | --- | --- |
| Stored `balance_minor` | **600** | Diagnostic script + failing assertion narrative |
| Ledger-derived customer sum | **700** | Same script |
| Funding rows for account | **3** | Same (1 initial + 2 concurrent) |
| Customer ledger entries | **3** | Same |
| Pytest expectation | `balance == 700` | Test source |
| Failed assertion (narrative) | observed 600 vs expected 700 | Documented in Experiment 002 README remediation; **raw terminal capture of `assert 600 == 700` not found** in `terminals/` at report time → cite as **session-recorded evidence**, not a preserved log file |

Diagnostic script output as recorded in experiment documentation:

```text
{'balance': 600, 'ledger': 700, 'fundings': 3, 'customer_entries': 3}
```

### 5.5 Interleaving timeline

Initial: balance 500 after laboratory fund; ledger customer sum 500; 1 funding; 1 customer credit entry.

| Step | Session A | Session B | Stored balance | Ledger-derived customer balance |
| --- | --- | --- | --- | --- |
| 1 | `get` → 500 in identity map | `get` → 500 in identity map | 500 | 500 |
| 2 | barrier | barrier | 500 | 500 |
| 3 | `FOR UPDATE` (no populate) — object still 500 | waits | 500 | 500 |
| 4 | absolute write 500+100 → **600**; ledger +100 | waits | 600 | 600 |
| 5 | commit | — | 600 | 600 |
| 6 | — | `FOR UPDATE` succeeds; object still **500** | 600 | 600 |
| 7 | — | absolute write 500+100 → **600**; ledger +100 | 600 | **700** |
| 8 | — | commit | **600** | **700** |

**Mechanism:** `SELECT FOR UPDATE` acquired the row lock but SQLAlchemy did not refresh already-loaded attributes without `populate_existing`. `_post` mutated the stale Python balance and flushed an absolute `UPDATE`. Both ledger credits inserted successfully; the second absolute write overwrote the first projection.

---

## 6. Corrected implementation

### 6.1 Current `_lock_in_id_order` (`app/ledger/service.py` 175–185)

```175:185:app/ledger/service.py
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
```

### 6.2 Current `_apply_delta` / `_post` (188–232)

Relative SQL:

```sql
UPDATE accounts
SET balance_minor = balance_minor + :delta,
    version = version + 1,
    updated_at = :now
WHERE id = :id
  AND (kind = 'treasury' OR balance_minor + :delta >= 0)  -- when delta < 0
RETURNING balance_minor, version, updated_at
```

If no row matches for a customer debit → `InsufficientFunds` and **no** ledger insert for that `_post`. ORM fields updated via `set_committed_value` after success.

### 6.3 Guarantees vs scenarios

| Scenario | Behavior | Evidence |
| --- | --- | --- |
| Account already loaded in session | `populate_existing` refreshes on lock | `test_transfer_after_the_account_was_loaded…`; `test_loaded_balance_cannot_spend…` |
| Concurrent funding after shared load | Relative updates accumulate | Concurrent funding test expects 700 |
| Debit matches 0 rows | `InsufficientFunds`; no ledger line; outer txn rolls back | Loaded-balance spend test; overspend |
| Exception after debit before credit | Entire txn rolls back | `test_failure_after_debit_rolls_back` |
| Commit OK, client loses response | Money moved; client may retry | **Not verified** by automated test; idempotency absent |

### 6.4 Autoflush

Funding / transfer flush the movement header before `_post`. Relative updates use `synchronize_session=False`. Lock queries run after prior loads; pending Funding/Transfer inserts are flushed before posts. **Not verified:** exhaustive autoflush ordering under every Session configuration beyond the passing tests.

---

## 7. Ledger and reconciliation

### 7.1 Sign convention

Positive `amount_minor` credits a customer; negative debits. Funding: treasury `−A`, customer `+A`. Transfer: source `−A`, destination `+A`.

### 7.2 Example (conceptual, matches API smoke in prior session)

| Event | Customer A | Customer B | Treasury | Notes |
| --- | --- | --- | --- | --- |
| Fund 500 | 500 | 0 | −500 | entries ±500 |
| Fund 200 | 700 | 0 | −700 | |
| Transfer 150 A→B | 550 | 150 | −700 | Prior HTTP smoke: `500 700 completed 550 150` |

### 7.3 `invariant_report` detectors (`app/ledger/reconcile.py`)

Covered by `test_reconciliation_detects_each_planted_anomaly` (line 357): missing transfer/funding entries, single-entry / uneven transfers, wrong accounts, currency mismatches, customer and treasury projection drift (`balance_discrepancy_minor`).

**Reconciliation is not real-time enforcement.** It runs when invoked (tests, end of benchmark cell).

### 7.4 Anomalies that can still look “paired”

- Absolute balance overwrite with intact ±pairs (naive): `unbalanced_transfers=0`, `ledger_sum=0`, but `balance_mismatches` / `balance_discrepancy_minor` > 0. **Observed** in JSON.
- Multiple compensating wrong-account pairs: detectors check per-movement accounts; exotic multi-movement collusion **Not verified**.
- `TRUNCATE` wiping evidence: bypasses immutability trigger.

---

## 8. Experimental strategies

| Strategy | File | Isolation | Locking | Updates | Retries | Business validation | Observed violations |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Naive | `strategies.py` `_naive` | RC | None | Absolute SET from prior SELECT | No | Balance check in Python | Hot-source drift 10500 / 36000; opposite drift 800–1400 |
| Conditional | `_conditional` | RC | Source then dest UPDATE locks | Relative with `balance >=` | No | Kind/status/open | Opposite 40P01/55P03; viol 0 |
| Optimistic | `_optimistic_once` | RC | UPDATE row locks | Absolute CAS on version | Up to 5 on conflict | Balance check | Exhaustion on hot source; opposite deadlocks |
| `pessimistic_sql` | `_pessimistic` | RC | FOR UPDATE sorted ids | Absolute from locked snapshot | No | Funds check only | viol 0 in corrected cells |
| `pessimistic_service` | `execute_transfer` | RC | FOR UPDATE + populate_existing | Relative conditional | No | Open customer, currency, funds | viol 0 in corrected cells |

**They are not identical.** Service validates currency and customer status; SQL twin does not. Service uses relative updates; SQL twin writes absolute balances from the locked snapshot. Benchmark service path does not traverse HTTP.

---

## 9. Benchmark methodology

### 9.1 Workloads

| Parameter | Original size | Larger |
| --- | --- | --- |
| Workers | 4 | 4 |
| Warmup iterations / worker | 10 | 20 |
| Measured iterations / worker | 25 | 100 |
| Amount | 100 | 100 |
| Repetitions | 3 | 3 |
| Hot source funding | `4*(warmup+iters)*100` | same formula |
| Opposite funding each side | `2*(warmup+iters)*100` | same |
| Opposite `lock_timeout` | `2s` (worker connection / `SET LOCAL`) | same |
| Strategies original size | all 5 | — |
| Strategies larger hot | all 5 | |
| Strategies larger opposite | **only** `pessimistic_sql`, `pessimistic_service` | |

### 9.2 Metrics

- **Logical operations:** `workers * iterations` (warmup excluded from logical count).
- **Successful / rejected / retries / exhausted:** from `StrategyResult`.
- **SQLSTATE map:** measured window (corrected files also expose `deadlock_errors`, `lock_timeouts`).
- **Deadlocks:** delta of `pg_stat_database.deadlocks`; corrected files split warmup vs measured.
- **Success latency:** committed ops only.
- **E2E latency (corrected):** every logical op including failures.
- **p50:** `statistics.median`; **p95/p99:** inclusive nearest rank `sorted[floor((n-1)*q)]`; p99 omitted if samples < 100.
- **Invariants:** `invariant_report` after cell; CLI exits 1 if `invariant_violations != 0`.
- Isolation of cells: each run `_clear` truncates financial tables (not `counters`).

---

## 10. Benchmark results

Source paths:

- Historical: `benchmark-results/experiment_002/*.json`
- Corrected: `benchmark-results/experiment_002_corrected/*.json`

Latency units: milliseconds. Throughput: successful transfers / second. Drift: `balance_discrepancy_minor` (corrected) or `balance_mismatches` count (historical; absolute drift **N/A** in historical schema).

### Corrected original-size hot-source

| File | Strategy | Logical | OK | Reject | Exhaust | SQLSTATE | Deadlocks | 40P01 | 55P03 | OK/s | s-p50 | s-p95 | s-p99 | e-p50 | e-p95 | e-p99 | Drift | UnbalT | Viol |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1-hot-conditional.json | conditional | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 527.921 | 7.389 | 11.267 | 13.908 | 7.389 | 11.267 | 13.908 | 0 | 0 | 0 |
| r1-hot-naive.json | naive | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 541.557 | 5.602 | 12.718 | 20.376 | 5.602 | 12.718 | 20.376 | 10500 | 0 | 1 |
| r1-hot-optimistic.json | optimistic | 100 | 79 | 0 | 21 | {} | 0 | 0 | 0 | 387.838 | 2.541 | 3.292 | N/A | 2.626 | 16.822 | 17.851 | 0 | 0 | 0 |
| r1-hot-pessimistic_service.json | pessimistic_service | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 402.259 | 7.832 | 16.242 | 21.097 | 7.832 | 16.242 | 21.097 | 0 | 0 | 0 |
| r1-hot-pessimistic_sql.json | pessimistic_sql | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 437.499 | 9.160 | 9.682 | 11.213 | 9.160 | 9.682 | 11.213 | 0 | 0 | 0 |
| r2-hot-conditional.json | conditional | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 529.236 | 7.297 | 12.354 | 15.182 | 7.297 | 12.354 | 15.182 | 0 | 0 | 0 |
| r2-hot-naive.json | naive | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 578.507 | 6.250 | 10.843 | 15.196 | 6.250 | 10.843 | 15.196 | 10500 | 0 | 1 |
| r2-hot-optimistic.json | optimistic | 100 | 79 | 0 | 21 | {} | 0 | 0 | 0 | 385.492 | 2.568 | 3.300 | N/A | 2.677 | 16.530 | 17.679 | 0 | 0 | 0 |
| r2-hot-pessimistic_service.json | pessimistic_service | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 342.638 | 11.308 | 14.237 | 14.879 | 11.308 | 14.237 | 14.879 | 0 | 0 | 0 |
| r2-hot-pessimistic_sql.json | pessimistic_sql | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 438.788 | 8.913 | 13.147 | 15.286 | 8.913 | 13.147 | 15.286 | 0 | 0 | 0 |
| r3-hot-conditional.json | conditional | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 538.835 | 6.682 | 13.488 | 16.395 | 6.682 | 13.488 | 16.395 | 0 | 0 | 0 |
| r3-hot-naive.json | naive | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 596.413 | 5.380 | 12.633 | 16.016 | 5.380 | 12.633 | 16.016 | 10500 | 0 | 1 |
| r3-hot-optimistic.json | optimistic | 100 | 79 | 0 | 21 | {} | 0 | 0 | 0 | 390.032 | 2.519 | 3.059 | N/A | 2.622 | 16.239 | 17.040 | 0 | 0 | 0 |
| r3-hot-pessimistic_service.json | pessimistic_service | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 384.535 | 10.264 | 13.155 | 14.592 | 10.264 | 13.155 | 14.592 | 0 | 0 | 0 |
| r3-hot-pessimistic_sql.json | pessimistic_sql | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 473.512 | 7.565 | 12.653 | 13.678 | 7.565 | 12.653 | 13.678 | 0 | 0 | 0 |

### Corrected original-size opposite

| File | Strategy | Logical | OK | Reject | Exhaust | SQLSTATE | Deadlocks | 40P01 | 55P03 | OK/s | s-p50 | s-p95 | s-p99 | e-p50 | e-p95 | e-p99 | Drift | UnbalT | Viol |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1-opposite-conditional.json | conditional | 100 | 30 | 0 | 0 | {'40P01': 48, '55P03': 22} | 48 | 48 | 22 | 0.573 | 2013.922 | 3020.580 | N/A | 2011.501 | 4014.417 | 4022.169 | 0 | 0 | 0 |
| r1-opposite-naive.json | naive | 100 | 39 | 0 | 0 | {'40P01': 49, '55P03': 12} | 49 | 49 | 12 | 0.730 | 2017.078 | 4014.232 | N/A | 2016.266 | 4014.354 | 4025.241 | 1000 | 0 | 2 |
| r1-opposite-optimistic.json | optimistic | 100 | 34 | 0 | 2 | {'40P01': 32, '55P03': 32} | 32 | 32 | 32 | 0.703 | 2014.790 | 3025.731 | N/A | 2008.708 | 3028.966 | 4024.012 | 0 | 0 | 0 |
| r1-opposite-pessimistic_service.json | pessimistic_service | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 377.968 | 10.425 | 12.518 | 13.477 | 10.425 | 12.518 | 13.477 | 0 | 0 | 0 |
| r1-opposite-pessimistic_sql.json | pessimistic_sql | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 507.745 | 7.818 | 8.614 | 10.003 | 7.818 | 8.614 | 10.003 | 0 | 0 | 0 |
| r2-opposite-conditional.json | conditional | 100 | 36 | 0 | 0 | {'40P01': 52, '55P03': 12} | 52 | 52 | 12 | 0.627 | 2022.385 | 4011.901 | N/A | 2017.123 | 4014.474 | 4028.801 | 0 | 0 | 0 |
| r2-opposite-naive.json | naive | 100 | 35 | 0 | 0 | {'40P01': 52, '55P03': 13} | 52 | 52 | 13 | 0.656 | 2016.260 | 3026.137 | N/A | 2014.654 | 4014.385 | 4025.936 | 1400 | 0 | 2 |
| r2-opposite-optimistic.json | optimistic | 100 | 39 | 0 | 3 | {'40P01': 35, '55P03': 23} | 35 | 35 | 23 | 0.898 | 1011.147 | 3036.209 | N/A | 2008.482 | 3031.796 | 4025.371 | 0 | 0 | 0 |
| r2-opposite-pessimistic_service.json | pessimistic_service | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 380.410 | 10.352 | 11.427 | 12.903 | 10.352 | 11.427 | 12.903 | 0 | 0 | 0 |
| r2-opposite-pessimistic_sql.json | pessimistic_sql | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 485.707 | 7.892 | 9.883 | 11.552 | 7.892 | 9.883 | 11.552 | 0 | 0 | 0 |
| r3-opposite-conditional.json | conditional | 100 | 34 | 0 | 0 | {'40P01': 50, '55P03': 16} | 50 | 50 | 16 | 0.604 | 2019.203 | 4012.263 | N/A | 2013.349 | 4013.706 | 5012.177 | 0 | 0 | 0 |
| r3-opposite-naive.json | naive | 100 | 35 | 0 | 0 | {'40P01': 48, '55P03': 17} | 48 | 48 | 17 | 0.668 | 2018.010 | 4013.080 | N/A | 2013.600 | 4021.090 | 4032.057 | 800 | 0 | 2 |
| r3-opposite-optimistic.json | optimistic | 100 | 32 | 0 | 0 | {'40P01': 38, '55P03': 30} | 37 | 38 | 30 | 0.648 | 2011.593 | 3025.302 | N/A | 2012.684 | 4015.645 | 4028.019 | 0 | 0 | 0 |
| r3-opposite-pessimistic_service.json | pessimistic_service | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 390.151 | 10.330 | 11.160 | 12.739 | 10.330 | 11.160 | 12.739 | 0 | 0 | 0 |
| r3-opposite-pessimistic_sql.json | pessimistic_sql | 100 | 100 | 0 | 0 | {} | 0 | 0 | 0 | 454.566 | 8.004 | 12.933 | 14.207 | 8.004 | 12.933 | 14.207 | 0 | 0 | 0 |

### Corrected larger hot-source

| File | Strategy | Logical | OK | Reject | Exhaust | SQLSTATE | Deadlocks | 40P01 | 55P03 | OK/s | s-p50 | s-p95 | s-p99 | e-p50 | e-p95 | e-p99 | Drift | UnbalT | Viol |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| large-r1-hot-conditional.json | conditional | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 648.430 | 5.825 | 10.926 | 15.245 | 5.825 | 10.926 | 15.245 | 0 | 0 | 0 |
| large-r1-hot-naive.json | naive | 400 | 400 | 0 | 0 | {} | 1 | 0 | 0 | 640.980 | 5.770 | 11.195 | 15.808 | 5.770 | 11.195 | 15.808 | 36000 | 0 | 1 |
| large-r1-hot-optimistic.json | optimistic | 400 | 312 | 0 | 88 | {} | 0 | 0 | 0 | 396.287 | 2.528 | 2.856 | 3.074 | 2.606 | 16.366 | 16.711 | 0 | 0 | 0 |
| large-r1-hot-pessimistic_service.json | pessimistic_service | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 442.026 | 8.894 | 10.871 | 13.126 | 8.894 | 10.871 | 13.126 | 0 | 0 | 0 |
| large-r1-hot-pessimistic_sql.json | pessimistic_sql | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 514.652 | 7.769 | 9.654 | 9.943 | 7.769 | 9.654 | 9.943 | 0 | 0 | 0 |
| large-r2-hot-conditional.json | conditional | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 665.960 | 5.800 | 10.471 | 15.470 | 5.800 | 10.471 | 15.470 | 0 | 0 | 0 |
| large-r2-hot-naive.json | naive | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 643.865 | 5.931 | 11.039 | 16.912 | 5.931 | 11.039 | 16.912 | 36000 | 0 | 1 |
| large-r2-hot-optimistic.json | optimistic | 400 | 312 | 0 | 88 | {} | 0 | 0 | 0 | 399.387 | 2.495 | 2.838 | 3.079 | 2.579 | 16.219 | 16.668 | 0 | 0 | 0 |
| large-r2-hot-pessimistic_service.json | pessimistic_service | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 469.652 | 6.564 | 14.398 | 18.922 | 6.564 | 14.398 | 18.922 | 0 | 0 | 0 |
| large-r2-hot-pessimistic_sql.json | pessimistic_sql | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 550.380 | 5.810 | 13.911 | 16.150 | 5.810 | 13.911 | 16.150 | 0 | 0 | 0 |
| large-r3-hot-conditional.json | conditional | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 658.140 | 5.916 | 10.442 | 12.140 | 5.916 | 10.442 | 12.140 | 0 | 0 | 0 |
| large-r3-hot-naive.json | naive | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 591.136 | 6.121 | 12.617 | 20.142 | 6.121 | 12.617 | 20.142 | 36000 | 0 | 1 |
| large-r3-hot-optimistic.json | optimistic | 400 | 312 | 0 | 88 | {} | 0 | 0 | 0 | 397.793 | 2.484 | 2.871 | 3.212 | 2.569 | 16.118 | 16.488 | 0 | 0 | 0 |
| large-r3-hot-pessimistic_service.json | pessimistic_service | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 458.279 | 8.699 | 10.830 | 11.345 | 8.699 | 10.830 | 11.345 | 0 | 0 | 0 |
| large-r3-hot-pessimistic_sql.json | pessimistic_sql | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 527.304 | 7.415 | 11.323 | 12.946 | 7.415 | 11.323 | 12.946 | 0 | 0 | 0 |

### Corrected larger opposite

| File | Strategy | Logical | OK | Reject | Exhaust | SQLSTATE | Deadlocks | 40P01 | 55P03 | OK/s | s-p50 | s-p95 | s-p99 | e-p50 | e-p95 | e-p99 | Drift | UnbalT | Viol |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| large-r1-opposite-pessimistic_service.json | pessimistic_service | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 430.774 | 9.239 | 10.418 | 11.891 | 9.239 | 10.418 | 11.891 | 0 | 0 | 0 |
| large-r1-opposite-pessimistic_sql.json | pessimistic_sql | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 512.918 | 7.782 | 8.269 | 9.754 | 7.782 | 8.269 | 9.754 | 0 | 0 | 0 |
| large-r2-opposite-pessimistic_service.json | pessimistic_service | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 430.463 | 9.234 | 11.049 | 11.808 | 9.234 | 11.049 | 11.808 | 0 | 0 | 0 |
| large-r2-opposite-pessimistic_sql.json | pessimistic_sql | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 515.783 | 7.730 | 9.556 | 10.001 | 7.730 | 9.556 | 10.001 | 0 | 0 | 0 |
| large-r3-opposite-pessimistic_service.json | pessimistic_service | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 432.562 | 9.197 | 9.805 | 11.561 | 9.197 | 9.805 | 11.561 | 0 | 0 | 0 |
| large-r3-opposite-pessimistic_sql.json | pessimistic_sql | 400 | 400 | 0 | 0 | {} | 0 | 0 | 0 | 503.534 | 7.774 | 9.694 | 11.929 | 7.774 | 9.694 | 11.929 | 0 | 0 | 0 |

### Historical original hot-source (phase experiment-002)

| File | Strategy | Logical | OK | Reject | Exhaust | SQLSTATE | Deadlocks | 40P01 | 55P03 | OK/s | p50 | p95 | p99 | e-p50 | e-p95 | e-p99 | Drift | UnbalT | Viol | Mismatches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1-conditional.json | conditional | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 561.500 | 6.837 | 12.343 | 14.169 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r1-naive.json | naive | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 502.924 | 7.158 | 13.552 | 17.197 | N/A | N/A | N/A | N/A | 0 | 1 | 1 |
| r1-optimistic.json | optimistic | 100 | 79 | 0 | 21 | {} | 0 | N/A | N/A | 382.584 | 2.602 | 4.870 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r1-pessimistic.json | pessimistic | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 516.649 | 6.041 | 12.993 | 19.435 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r2-conditional.json | conditional | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 645.241 | 4.668 | 12.031 | 15.361 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r2-naive.json | naive | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 618.375 | 4.659 | 11.310 | 14.334 | N/A | N/A | N/A | N/A | 0 | 1 | 1 |
| r2-optimistic.json | optimistic | 100 | 79 | 0 | 21 | {} | 0 | N/A | N/A | 387.211 | 2.519 | 3.487 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r2-pessimistic.json | pessimistic | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 505.879 | 7.869 | 8.654 | 9.995 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r3-conditional.json | conditional | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 624.149 | 5.977 | 10.523 | 12.796 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r3-naive.json | naive | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 624.196 | 5.434 | 10.921 | 18.155 | N/A | N/A | N/A | N/A | 0 | 1 | 1 |
| r3-optimistic.json | optimistic | 100 | 79 | 0 | 21 | {} | 0 | N/A | N/A | 389.244 | 2.561 | 3.120 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r3-pessimistic.json | pessimistic | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 503.956 | 7.892 | 9.725 | 10.354 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |

### Historical original opposite (phase experiment-002)

| File | Strategy | Logical | OK | Reject | Exhaust | SQLSTATE | Deadlocks | 40P01 | 55P03 | OK/s | p50 | p95 | p99 | e-p50 | e-p95 | e-p99 | Drift | UnbalT | Viol | Mismatches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1-opposite-conditional.json | conditional | 100 | 34 | 0 | 0 | {'40P01': 48, '55P03': 18} | 67 | 48 | 18 | 0.626 | 2017.044 | 4010.523 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r1-opposite-naive.json | naive | 100 | 36 | 0 | 0 | {'40P01': 48, '55P03': 16} | 67 | 48 | 16 | 0.675 | 2014.658 | 3020.443 | N/A | N/A | N/A | N/A | N/A | 0 | 2 | 2 |
| r1-opposite-optimistic.json | optimistic | 100 | 31 | 0 | 0 | {'40P01': 39, '55P03': 30} | 54 | 39 | 30 | 0.628 | 2011.837 | 3027.209 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r1-opposite-pessimistic.json | pessimistic | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 442.945 | 9.150 | 9.882 | 11.342 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r2-opposite-conditional.json | conditional | 100 | 30 | 0 | 0 | {'40P01': 49, '55P03': 21} | 68 | 49 | 21 | 0.533 | 2017.146 | 3020.888 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r2-opposite-naive.json | naive | 100 | 33 | 0 | 0 | {'40P01': 54, '55P03': 13} | 73 | 54 | 13 | 0.556 | 2019.403 | 4011.321 | N/A | N/A | N/A | N/A | N/A | 0 | 2 | 2 |
| r2-opposite-optimistic.json | optimistic | 100 | 33 | 0 | 0 | {'40P01': 37, '55P03': 30} | 51 | 37 | 30 | 0.712 | 1014.522 | 4022.371 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r2-opposite-pessimistic.json | pessimistic | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 445.118 | 9.069 | 9.730 | 11.740 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r3-opposite-conditional.json | conditional | 100 | 38 | 0 | 0 | {'40P01': 53, '55P03': 9} | 73 | 53 | 9 | 0.662 | 2017.383 | 3033.003 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r3-opposite-naive.json | naive | 100 | 38 | 0 | 0 | {'40P01': 48, '55P03': 14} | 67 | 48 | 14 | 0.739 | 2016.513 | 3019.886 | N/A | N/A | N/A | N/A | N/A | 0 | 2 | 2 |
| r3-opposite-optimistic.json | optimistic | 100 | 44 | 0 | 5 | {'40P01': 32, '55P03': 19} | 42 | 32 | 19 | 1.115 | 20.054 | 3014.839 | N/A | N/A | N/A | N/A | N/A | 0 | 0 | 0 |
| r3-opposite-pessimistic.json | pessimistic | 100 | 100 | 0 | 0 | {} | 0 | N/A | N/A | 428.799 | 9.298 | 11.072 | 11.740 | N/A | N/A | N/A | N/A | 0 | 0 | 0 |


### Notes on reading the tables

- Historical cells labeled strategy `pessimistic` are the SQL twin later renamed **`pessimistic_sql`** in the corrected runner; they are **not** `pessimistic_service`.
- Historical files lack `balance_discrepancy_minor`, e2e latency, and warmup deadlock split → shown as **N/A**.
- Optimistic hot-source success p99 is **N/A** when fewer than 100 successes (79 samples).
- Corrected naive hot-source: OK=100 or 400 with Drift 10500 or 36000 and Viol=1 — **correctness anomaly**, not a performance win.
- `large-r1-hot-naive.json` recorded Deadlocks=1 with empty SQLSTATE and OK=400 — **inconclusive** counter movement.

---

## 11. Statistical and methodological limitations

- Sample: 3 repetitions; 100 or 400 logical ops; one machine; Docker VM 2 CPUs / ~2 GiB.
- Success-only latency understates cost when exhaustion/deadlocks occur (optimistic, unordered opposite).
- `pessimistic_service` ≠ HTTP latency.
- Ordered locking evidence is for **two-account** opposite transfers only.
- Unexplained deadlock counter tick on one naive cell.
- Laboratory residue can affect tests that assert **global** `violations()==0` if tables are not truncated before the suite (fixture cleans after each test).

Correctness conclusions (lost update, remediation, pessimistic invariant preservation) are stronger than performance rankings.

---

## 12. Automated test evidence

### 12.1 Coverage matrix

| Test | Lines | Scenario | Postgres | Barrier | Expected |
| --- | --- | --- | --- | --- | --- |
| `test_account_fund_transfer_and_entries` | 46–70 | Happy path + 404 | Yes | No | Balances/entries; viol=0 |
| `test_rejects_invalid_transfer_requests` | 73–111 | 422/409; no transfer row | Yes | No | Balance 500; transfers=0 |
| `test_concurrent_overspend_allows_exactly_one_transfer` | 114–140 | 800+800 from 1000 | Yes | Yes | Results [0,1]; source 200 |
| `test_parallel_transfers_with_sufficient_funds` | 143–161 | 4×100 from 1000 | Yes | Yes | Source 600 |
| `test_failure_after_debit_rolls_back` | 164–187 | Hook raises | Yes | No | Unchanged; transfers=0 |
| `test_ledger_entries_are_immutable_and_reconciliation_detects_drift` | 190–218 | Trigger + drift | Yes | No | immutable; drift detected then rolled back |
| `test_opposite_transfers_do_not_deadlock` | 221–246 | 10 each way | Yes | Yes | Balances 500; deadlock delta 0 |
| `test_concurrent_funding_uses_the_balance_loaded_before_the_lock` | 249–280 | Stale-load fund race | Yes | Yes (pre-lock) | Balance 700; ledger 700 |
| `test_concurrent_funding_and_transfer_keep_one_balance` | 283–315 | Fund+transfer | Yes | Yes | 350 / 100; viol=0 |
| `test_loaded_balance_cannot_spend_funds_removed_by_another_transaction` | 318–337 | Stale spend | Yes | No | InsufficientFunds; one transfer |
| `test_transfer_after_the_account_was_loaded_in_this_session` | 340–354 | Load then concurrent fund then transfer | Yes | No | 550 / 30 |
| `test_reconciliation_detects_each_planted_anomaly` | 357–545 | Planted faults | Yes | No | Each detector ≥1; final viol=0 |

All marked `@pytest.mark.postgres`; skip without `DATABASE_URL`.

### 12.2 Independent quality execution (this report)

```text
=== RUFF ===
All checks passed!
EXIT:0

=== MYPY ===
Success: no issues found in 27 source files
EXIT:0

=== PYTEST (first run) ===
1 failed, 27 passed
FAILED tests/test_ledger.py::test_account_fund_transfer_and_entries
assert 1 == 0  (violations)
EXIT:1

=== After TRUNCATE ledger_entries, fundings, transfers, accounts ===
28 passed, 1 warning
EXIT:0

=== ALEMBIC ===
002 (head)
EXIT:0
```

**Interpretation:** The first failure’s residual state was not available after fixture teardown. A clean laboratory schema yielded a full green suite. Treat global reconciliation assertions as sensitive to leftover laboratory rows.

Experiment 001 tests were included in the 28 and passed on the clean run.

---

## 13. Critical review and counterexamples

| # | Question | Finding | Severity | Tests | Uncertainty |
| --- | --- | --- | --- | --- | --- |
| 1 | Stale ORM elsewhere? | Funding/transfer locks now populate_existing; relative updates | Medium historically | Concurrent funding + loaded-session tests | Other code paths that mutate balances without `_apply_delta` would be a regression risk; strategies intentionally unsafe |
| 2 | Commit without ledger pair? | Possible if SQL inserted transfer without entries outside service | High if done | Planted missing entries detected by reconcile | Service path posts under one txn; DB does not forbid header-only transfer |
| 3 | Reconcile miss? | Paired wrong absolute projection still flagged via mismatch/drift | Medium | Naive JSON + planted tests | Sophisticated multi-row fraud **Not verified** |
| 4 | Wrong currency credit? | Service rejects mismatch; SQL strategies weaker | Medium | API 409 test | Benchmark SQL twin can copy source currency |
| 5 | Concurrent fund+transfer lost update? | Test expects reconciled 350/100 | Medium | Dedicated test | Broader schedules **Not verified** |
| 6 | Deadlocks beyond 2 accounts? | Ordered lock helps 2-id case only | Medium | Opposite test + benchmarks | ADR states non-universal |
| 7 | SQL misclassified as business reject? | `_Rejected` vs `DBAPIError` sqlstate | Low | Strategy result fields | Mapping mistakes would show in JSON |
| 8 | Commit without HTTP ack? | Possible | Medium | **Not verified** | Idempotency not implemented |
| 9 | Duplicate POST doubles money? | Yes by design today | High for product | **Not verified** as prevented | Explicit next experiment |
| 10 | Zero discrepancy hides other break? | violation_count sums multiple detectors | Medium | Planted tests | Operator must read full report, not one field |

---

## 14. Claims we can safely publish

| Proposed claim | Evidence | Confidence | Required qualification |
| --- | --- | --- | --- |
| A DB lock does not automatically refresh SQLAlchemy ORM state | Archive original lock without `populate_existing`; SQLAlchemy loader behavior; reproduced 600 vs 700 | High | Specific to identity-map + missing populate_existing |
| Original funding path lost an update under concurrency | Reproduction test design + diagnostic dict | High | Uncommitted code; raw pytest log file not archived |
| Race committed ledger entries with wrong balance | fundings=3, entries=3, ledger=700, balance=600 | High | Same |
| Relative updates + refreshed locks fixed the reproduced failure | Corrected code; same test now expects 700 and passes on clean suite | High | Independent suite needs clean tables |
| Naive hot-source committed all measured ops while violating balances | Corrected JSON Drift 10500/36000, OK=100/400 | High | Environment-specific magnitudes |
| Balanced ledger ≠ correct projection | Naive + funding race | High | — |
| Ordered pessimistic locking prevented deadlocks in tested opposite workloads | JSON deadlocks=0 for sql+service; opposite test | High | Two-account workloads only |
| Public transfer service preserved invariants in measured workloads | `pessimistic_service` viol=0, drift=0 all corrected cells | High | Not HTTP; not all possible workflows |
| SQL pessimistic faster than service here | Throughput tables | Medium | One machine; not a general ranking |
| Results do not prove universal strategy superiority | Methodology + naive correctness failure | High | — |

---

## 15. Final independent assessment

### Confirmed findings

1. Concurrent funding with a pre-lock identity-map load produced stored balance 600 against ledger 700 (session-recorded measurements).
2. Corrected service uses ordered `FOR UPDATE` with `populate_existing` and relative conditional balance updates (`app/ledger/service.py`).
3. PostgreSQL constraints do not enforce double-entry pairing; reconciliation detects planted pairing/account/currency/missing-entry faults.
4. Corrected `pessimistic_service` and `pessimistic_sql` show zero invariant violations and zero measured deadlocks on recorded hot-source and opposite cells (original and larger sizes where executed).
5. Naive strategy repeatedly commits full logical counts while leaving nonzero balance discrepancy.
6. Clean independent verification: Ruff, mypy, Alembic 002, pytest 28 passed.

### Remaining risks

- Uncommitted Experiment 002 tree (no implementation SHA).
- Idempotency absent; duplicate POST can double-spend.
- Ledger pairing not DB-enforced.
- Fixture truncates after tests; dirty laboratory DB can fail global violation asserts.
- Benchmark ≠ HTTP; Docker undersized vs host.
- One unexplained deadlock-counter increment on a naive large cell.

### Unverified claims

- Raw filesystem preservation of the original `assert 600 == 700` pytest log.
- Production traffic behavior.
- Deadlock freedom for >2 account lock sets.
- Automatic recovery after commit-ack loss.
- That the first independent pytest failure was caused by a specific leftover row set (tables empty after teardown).

### Publication readiness

**READY WITH QUALIFICATIONS**

Evidence supports a technically accurate article on: ORM lock refresh pitfalls, a reproduced funding lost update, the remediation, naive projection failure despite balanced pairs, and ordered locking behavior under the measured workloads—provided claims stay scoped to this schema, these tests, these JSON files, and the uncommitted working tree, and provided idempotency / HTTP / universal ranking claims are excluded or explicitly marked future work.

---

## Appendix A — Key file line index

| Topic | Location |
| --- | --- |
| Fund + transfer service | `app/ledger/service.py` |
| HTTP contracts | `app/ledger/api.py` |
| Models | `app/ledger/models.py` |
| Reconciliation | `app/ledger/reconcile.py` |
| Migration | `alembic/versions/002_create_ledger.py` |
| Strategies | `experiments/experiment_002/strategies.py` |
| Benchmark | `experiments/experiment_002/benchmark.py` |
| Tests | `tests/test_ledger.py` |
| ADR | `docs/adr/0003-transfer-locking-and-ledger.md` |

## Appendix B — Evidence integrity statement

Benchmark JSON files were read directly from disk for Section 10 tables. Schema was dumped from the live PostgreSQL instance. Quality commands were re-executed for Section 12. Original pre-fix `_post` / `_lock_in_id_order` text was recovered from the agent session archive because git does not contain that revision. Where evidence is missing, this report labels it **Not verified** rather than inferring it.
