# Engineering Beyond Assumptions

**Real systems. Reproducible failures. Measured trade-offs.**

A public engineering laboratory for testing architectural decisions with working code, controlled experiments, and documented evidence. The project begins with a Python/FastAPI backend and PostgreSQL concurrency experiments, with a longer-term path toward messaging systems, reliability engineering, and applied AI.

The goal is not to demonstrate that one technology is universally better than another. It is to ask a precise question, reproduce the behavior, measure what happened, and explain the trade-offs—including results that challenge the original hypothesis.

> **Engineering principle:** A successful test proves the behavior it tests, not every property we wish the system had. Keep correctness, throughput, latency, and operational cost separate when evaluating a design.

## What you'll find here

- **Working backend:** a small FastAPI application with PostgreSQL-backed counters.
- **Reproducible experiments:** scripts, tests, benchmark procedures, and reported results.
- **Failure analysis:** deliberate reproduction of concurrency anomalies before comparing alternatives.
- **Engineering decisions:** architecture decision records (ADRs) and experiment limitations.
- **Future investigations:** message delivery, retries, Kafka and RabbitMQ, observability, and AI systems. These are roadmap topics, **not implemented capabilities**.

## Experiment catalog

| Experiment | Question | Status | Evidence |
| --- | --- | --- | --- |
| **001 — PostgreSQL concurrent writes** | What happens when concurrent transactions update a shared counter? | Phase 1 executed; further investigation in progress | [Protocol and results](experiments/experiment_001/README.md) |

### Experiment 001: correctness before performance

A counter is deliberately simple, making concurrency behavior easier to isolate. The application endpoint uses an atomic database update; the experimental suite also includes a deliberately unsafe read–modify–write strategy to demonstrate lost updates.

The initial reported test run found:

- A barrier-controlled **lost-update test reproduced the anomaly in 30/30 repetitions** under `READ COMMITTED`: two transactions read `0`, each wrote `1`, and the final value was `1` rather than `2`.
- **Atomic updates preserved all 200 increments** in the correctness test with eight workers and 25 iterations each.
- In the initial benchmark, atomic updates under `READ COMMITTED` preserved all successful increments for worker counts of 1, 2, 4, 8, and 16.
- At higher isolation levels, PostgreSQL rejected conflicting attempts with **SQLSTATE `40001`**. The initial benchmark did not retry those attempts; they must not be mistaken for successful business operations.

These findings come from the initial local test report; detailed workload parameters, raw data locations, and limitations belong in the [experiment protocol](experiments/experiment_001/README.md). The results are **environment-specific**, not production throughput claims. Follow-up work examines retries, contention, and how workload distribution changes performance. No Phase 2 result is claimed here before it is verified and documented.

**Why it matters:** a benchmark can report committed transactions while application-level updates are lost. Performance comparisons only make sense after correctness requirements are explicit.

## Architecture at a glance

```text
Client / test runner
        |
        v
    FastAPI (Python)
        |
        v
 PostgreSQL (counters)

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

See **[Experiment 001: protocol, commands, results, and limitations](experiments/experiment_001/README.md)** for the current benchmark procedure. Run the tests and benchmarks using the documented configuration rather than relying on results from another machine.

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

- **Concurrency and data integrity:** isolation levels, retries, hot rows, lock contention, and backpressure.
- **Messaging:** RabbitMQ versus Kafka for defined workload requirements; delivery semantics, replay, consumer recovery, and idempotency.
- **Reliability and observability:** tracing, business-level outcomes, failure injection, and reconciliation.
- **Applied AI:** secure tool execution, RAG evaluation, agent orchestration, cost/latency trade-offs, and deterministic safeguards.
- **Engineering decision-making:** compare hypotheses with evidence instead of adopting technology based on popularity or authority.

## About this project

Engineering Beyond Assumptions is built as a practical, public portfolio of backend engineering work. It reflects an approach grounded in hands-on implementation, production reliability, and explicit technical trade-offs. All example data and scenarios are synthetic; they are not a reproduction of proprietary systems.

**Start with the evidence:** [Experiment 001](experiments/experiment_001/README.md) · [Architecture decisions](docs/adr)
