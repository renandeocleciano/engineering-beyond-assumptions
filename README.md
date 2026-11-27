# Engineering Beyond Assumptions

**Real systems. Reproducible failures. Measured trade-offs.**

A public engineering laboratory for testing architectural decisions with working code, controlled experiments, and documented evidence. The project begins with a Python/FastAPI backend and PostgreSQL concurrency experiments, with a longer-term path toward messaging systems, reliability engineering, and applied AI.

The goal is not to demonstrate that one technology is universally better than another. It is to ask a precise question, reproduce the behavior, measure what happened, and explain the trade-offs—including results that challenge the original hypothesis.

> **Engineering principle:** A successful test proves the behavior it tests, not every property we wish the system had. Keep correctness, throughput, latency, and operational cost separate when evaluating a design.

## What you'll find here

- **Working backend:** a FastAPI application with PostgreSQL-backed counters and, in progress, accounts, transfers, and a signed ledger.
- **Reproducible experiments:** scripts, tests, benchmark procedures, and reported results.
- **Failure analysis:** deliberate reproduction of concurrency anomalies before comparing alternatives.
- **Engineering decisions:** architecture decision records (ADRs) and experiment limitations.
- **Future investigations:** message delivery, Kafka and RabbitMQ, observability, and AI systems. These are roadmap topics, **not implemented capabilities**. Transaction retries for SQLSTATE `40001` are already part of Experiment 001.

## Experiment catalog

| Experiment | Question | Status | Evidence |
| --- | --- | --- | --- |
| **001 — PostgreSQL concurrent writes** | What happens when concurrent transactions update a shared counter? | Phase 1 and Phase 2 recorded | [Protocol and results](experiments/experiment_001/README.md) |
| **002 — Transaction API** | Which transfer strategies keep balances and ledger entries correct under concurrency? | Recorded | [Protocol and results](experiments/experiment_002/README.md) |
| **003 — Idempotency under concurrency** | What happens when a transfer commits but the client times out or retries? | Recorded | [Protocol and results](experiments/experiment_003/README.md) |

### Experiment 001: correctness before performance

A counter is deliberately simple, making concurrency behavior easier to isolate. The application endpoint uses an atomic database update; the experimental suite also includes a deliberately unsafe read–modify–write strategy to demonstrate lost updates.

The initial reported test run found:

- A barrier-controlled **lost-update test reproduced the anomaly in 30/30 repetitions** under `READ COMMITTED`: two transactions read `0`, each wrote `1`, and the final value was `1` rather than `2`.
- **Atomic updates preserved all 200 increments** in the correctness test with eight workers and 25 iterations each.
- In the initial benchmark, atomic updates under `READ COMMITTED` preserved all successful increments for worker counts of 1, 2, 4, 8, and 16.
- At higher isolation levels, PostgreSQL rejected conflicting attempts with **SQLSTATE `40001`**. The initial benchmark did not retry those attempts; they must not be mistaken for successful business operations.

These findings come from the initial local test report. The results are **environment-specific**, not production throughput claims.

### Phase 2: retries and counter distribution

Phase 2 is recorded. It kept the atomic and read–modify–write strategies, added a bounded retry of the **whole transaction** after SQLSTATE `40001` (5 retries, full-jitter backoff from 1 ms capped at 50 ms), and compared 1, 10, and 100 counters. The measured workload was 8 workers, 50 warmup iterations, and 500 measured iterations per worker, repeated 3 times at all three isolation levels with retry budgets of 0 and 5: **108 cells**, 4,000 logical operations each, on 2026-11-24 01:40:40–01:43:02 UTC. Warmup is excluded from the totals. Every measurement-window error was `40001`, `other_failures` was 0, and `deadlocks` was 0.

On this machine the run showed:

- **Atomic updates at `READ COMMITTED`** committed all 4,000 operations and lost none. One shared counter ran at about 3,100–3,200 committed operations/s, with p99 around 8–9 ms. Ten and 100 counters ran at about 5,000–6,000 operations/s, with p99 around 2–4 ms. The retry budget was idle: PostgreSQL raised no `40001`.
- **Read–modify–write at `READ COMMITTED`** also committed all 4,000 operations and retried none. It still lost updates: about 3,390–3,428 on one counter, 903–1,124 on ten, and 378–684 on one hundred. Retries do not repair a lost update that was committed.
- **At `REPEATABLE READ` and `SERIALIZABLE`**, lost updates stayed 0. Without retries, the hot row committed only about 990–1,012 atomic operations, or 718–743 read–modify–write operations, out of 4,000. With 5 retries, the atomic hot row reached about 3,742–3,774 commits and still exhausted 226–258 operations. Spreading the same work across 10 or 100 counters let retries commit nearly all 4,000 operations.
- **Lock samples** (`pg_stat_activity` and ungranted `pg_locks`, every 50 ms) peaked at 6 or 7 waiting sessions on the single counter and at 0–3 sessions when the work was spread. `log_lock_waits` was on and recorded no `still waiting` lines, because waits were shorter than the 1 second `deadlock_timeout`. Lock-wait time was not measured.

Cell-by-cell counts, latency, commands, and limitations are in the [experiment protocol](experiments/experiment_001/README.md). The retry cap was fixed before the run. It was not enough to commit every hot-row operation at the stronger isolation levels. Eight workers oversubscribe the 2-CPU Colima VM, and the client reaches PostgreSQL through the host port forward.

**Why it matters:** a benchmark can report committed transactions while application-level updates are lost. A retry count is not a successful logical operation, and a higher throughput figure is not evidence that the counter is correct.

### Experiment 002: transfers and a ledger

Experiment 002 is **recorded**. It keeps Experiment 001 intact and adds fictional accounts, laboratory funding, synchronous transfers, and a signed double-entry ledger. Amounts are integer minor units. The public path locks the two accounts in identifier order, reloads any account already present in the session, and applies the balance change with a relative SQL update.

A test that loaded the same balance in two sessions and then funded both reproduced a stale write: the ledger summed to 700 and the stored balance was 600. After the fix, that test reconciles. The corrected benchmark calls `execute_transfer` directly as `pessimistic_service` and keeps the raw-SQL lock strategy as `pessimistic_sql`. On 2026-11-25 both committed every measured transfer in the hot-source and opposite-direction cells, with no balance discrepancy and no measured deadlocks. The naive strategy still committed its transfers and still left a balance that did not match the ledger. Details, including the first comparison, are in the [experiment protocol](experiments/experiment_002/README.md).

### Experiment 003: idempotency under concurrency and failure

Experiment 003 is **recorded**. `POST /transfers` requires an `Idempotency-Key`. The reservation and transfer commit in one PostgreSQL transaction under a unique `(scope, key)` constraint. On 2026-11-27, sequential and concurrent equivalent retries produced one transfer per key; lost-response recovery replayed the original result 20/20 times. Unsafe retries and a deterministic naive check-then-insert race duplicated movements while still reconciling per-transfer ledger invariants.

Publication evidence (schema v2 JSON) lives under [`experiments/experiment_003/results/`](experiments/experiment_003/results/). Benchmarks are **service-level**, **single-run**, and **not HTTP throughput**. Traffic cells use 2 warmup + 40 measured iterations; `committed_transfers` includes warmup rows. Keys are laboratory-wide with **no authentication**—a non-production limitation. Protocol, reproduction commands, and limitations: [Experiment 003](experiments/experiment_003/README.md). Independent audit: [technical evidence report](docs/experiment-003-technical-evidence-report.md).

## Architecture at a glance

```text
Client / test runner
        |
        v
    FastAPI (Python)
        |
        v
 PostgreSQL (counters, accounts, ledger)

Experiment runners exercise alternative concurrent-write
strategies against the database and record their outcomes.
```

This is intentionally a small starting architecture. Kafka, RabbitMQ, agents, and additional services will be introduced only when an experiment has a clear reason to need them.

### Current technology

| Area | Technology |
| --- | --- |
| Language and API | Python 3.13+, FastAPI |
| Persistence | PostgreSQL, SQLAlchemy |
| Schema migrations | Alembic |
| Local environment | Docker Compose |
| Tests and quality checks | Pytest, Ruff, mypy |
| CI | GitHub Actions |

## Getting started

### Option A — Docker Compose

**Prerequisite:** Docker with Compose support.

From the repository root:

```sh
docker compose up --build
```

The application applies its database migrations at startup. Open the interactive API documentation at **http://localhost:8000/docs**.

Try the counter API:

```sh
curl -X POST http://localhost:8000/counters \
  -H 'Content-Type: application/json' \
  -d '{"name":"example"}'

curl -X POST http://localhost:8000/counters/example/increment
curl http://localhost:8000/counters/example
```

The transaction API uses integer minor units. Creating an account and funding it:

```sh
curl -X POST http://localhost:8000/accounts \
  -H 'Content-Type: application/json' \
  -d '{"currency":"USD"}'
```

`POST /accounts/{account_id}/fund` adds synthetic laboratory funds and writes a balancing treasury entry. It is not a production deposit.

`POST /transfers` requires an `Idempotency-Key` header (Experiment 003). Replays of the same key and payload return the original transfer; a different payload with the same key returns `409`.

`GET /health` checks **process liveness**, not database readiness. Compose persists PostgreSQL data in a named volume. The example credentials and published ports are intended **only for local development**.

### Option B — Local Python environment

**Prerequisites:** Python 3.13+ and a PostgreSQL database. Start the database container if needed:

```sh
docker compose up -d db
```

Then:

```sh
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

export DATABASE_URL='postgresql+psycopg://experiments:experiments@localhost:5434/experiments'
alembic upgrade head

ruff check .
mypy
pytest
```

The documented local database URL uses **port 5434** (the initial development environment already had another PostgreSQL instance on port 5432). Confirm your own Compose port mapping before reusing it.

**Important:** PostgreSQL-marked tests intentionally skip unless `DATABASE_URL` is explicitly set. An otherwise green test command does not establish database correctness if these tests were skipped. To run the PostgreSQL tests explicitly:

```sh
pytest -m postgres
```

Use a **dedicated test database**. Tests create and delete rows; do not point them at production or valuable data. CI sets `DATABASE_URL` to run the full test suite.

### Database migrations

The current schema is managed through Alembic. To create and apply a future migration:

```sh
alembic revision --autogenerate -m 'description'
# Review the generated migration before applying it.
alembic upgrade head
```

## Reproduce the experiments

See **[Experiment 001](experiments/experiment_001/README.md)**, **[Experiment 002](experiments/experiment_002/README.md)**, and **[Experiment 003](experiments/experiment_003/README.md)** for the benchmark procedures. Experiment 003 reproduction writes JSON under `experiments/experiment_003/results/v2-publication/` (see that README). Run the tests and benchmarks using the documented configuration rather than relying on results from another machine.

When comparing runs, record at least:

- PostgreSQL version, CPU/memory limits, client location, and dataset;
- transaction strategy, isolation level, worker count, and iterations;
- successful logical operations, failed attempts, and retries separately;
- throughput, latency distribution, correctness checks, and relevant database errors;
- warmup, repetitions, instrumentation gaps, and limitations.

Never interpret a high throughput figure as success if the data is incorrect, or treat one short local run as a production capacity estimate.

## How decisions are documented

Each investigation should follow a consistent sequence:

1. **Problem:** describe an observable failure or engineering constraint.
2. **Hypothesis:** make a falsifiable prediction.
3. **Baseline:** capture the existing behavior and workload.
4. **Experiment:** change a controlled variable and rerun.
5. **Evidence:** retain measurements, tests, and known limitations.
6. **Decision:** explain what the results support, what they do not, and when the decision should be revisited.

Architectural choices are captured in **[Architecture Decision Records](docs/adr)**. The experiment reports document how to reproduce observations, rather than presenting benchmark numbers without context.

## Roadmap

The following are **planned areas of investigation**, not a promise that each technology will become a permanent part of the application:

- **Concurrency and data integrity:** backpressure and workloads beyond the idempotency cases measured in Experiment 003.
- **Messaging:** RabbitMQ versus Kafka for defined workload requirements; delivery semantics, replay, consumer recovery, and idempotency.
- **Reliability and observability:** tracing, business-level outcomes, failure injection, and reconciliation.
- **Applied AI:** secure tool execution, RAG evaluation, agent orchestration, cost/latency trade-offs, and deterministic safeguards.
- **Engineering decision-making:** compare hypotheses with evidence instead of adopting technology based on popularity or authority.

## About this project

Engineering Beyond Assumptions is built as a practical, public portfolio of backend engineering work. It reflects an approach grounded in hands-on implementation, production reliability, and explicit technical trade-offs. All example data and scenarios are synthetic; they are not a reproduction of proprietary systems.

**Start with the evidence:** [Experiment 001](experiments/experiment_001/README.md) · [Experiment 002](experiments/experiment_002/README.md) · [Experiment 003](experiments/experiment_003/README.md) · [Architecture decisions](docs/adr)
