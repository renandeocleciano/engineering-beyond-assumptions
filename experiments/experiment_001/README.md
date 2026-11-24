# Experiment 001: Concurrent writes to a hot row

## Question and hypotheses

What happens to correctness and throughput when concurrent transactions increment the same PostgreSQL row? At READ COMMITTED, a client-side read–modify–write can overwrite a committed increment. An atomic `UPDATE ... SET value = value + 1` should preserve every committed increment. At REPEATABLE READ and SERIALIZABLE, PostgreSQL may reject conflicting transactions with serialization failures; applications must retry the entire transaction if they require every attempt to succeed. This benchmark deliberately does not retry, so failures remain visible.

## Protocol

Start and migrate PostgreSQL as described in the root README. Set `DATABASE_URL` explicitly and use a dedicated database. Run a matrix with identical worker and iteration counts, e.g.:

```sh
python -m experiments.experiment_001.benchmark --workers 8 --iterations 100 --strategy atomic --isolation 'READ COMMITTED'
python -m experiments.experiment_001.benchmark --workers 8 --iterations 100 --strategy read-modify-write --isolation 'READ COMMITTED'
python -m experiments.experiment_001.benchmark --workers 8 --iterations 100 --strategy atomic --isolation 'SERIALIZABLE'
python -m experiments.experiment_001.benchmark --workers 8 --iterations 100 --strategy read-modify-write --isolation 'SERIALIZABLE'
```

Repeat runs with 1, 2, 4, 8, and 16 workers and both strategies at all three isolation levels. Save each JSON output to a file outside version control, record machine specifications, PostgreSQL configuration, concurrent load, and whether PostgreSQL is local or containerized. Compare repeated runs on the same environment. Avoid comparing numbers from different environments as if they were controlled measurements.

Each worker holds one connection and each increment is a separate transaction. All workers target one fresh row, creating a hot spot; the row is removed afterward. The output reports attempts, commits, database errors grouped by SQLSTATE, final value, lost updates (commits minus final value), the `pg_stat_database.deadlocks` delta for this database across the concurrent section, elapsed time, committed throughput, and transaction latency in milliseconds. Median latency uses `statistics.median`. p95 uses the inclusive nearest rank `sorted[floor((n - 1) * 0.95)]`. p99 uses the same estimator at 0.99 when there are at least 100 attempts; smaller samples omit it. Latencies include failed attempts. Elapsed time includes thread startup and the barrier, after connections are checked out. Failures include any database error. Lock-wait duration is unavailable on this image: `pg_stat_statements` is not loaded, `track_io_timing` is off, and `log_lock_waits` is off. Inspect database logs if errors occur. The benchmark does not model network clients, multiple rows, retries, warmup, or production load; treat it as an investigation, not a capacity estimate. An interrupted process may leave a `bench-` row behind.

The PostgreSQL tests include a barrier-controlled lost update at READ COMMITTED and a concurrent atomic-update correctness check. Run `pytest -m postgres` after migrations. The barrier makes the lost-update schedule deterministic rather than relying on timing. Benchmark outcomes, including whether a particular schedule produces lost updates or serialization failures, are observations to measure, not guaranteed results.

## Recorded run

This section records one execution of the matrix above. Raw JSON is in `benchmark-results/experiment_001/`, which is gitignored. Throughput is rounded to 0.1 commits/s, latency to 0.01 ms, and elapsed time to 0.001 s. Counts are exact. Every cell used 100 iterations per worker, so every cell has at least 100 latency samples and reports p99.

### Environment

| Item | Value |
|---|---|
| When (UTC) | 2026-11-24 01:28:38 through 01:28:59 |
| Client host | Apple M5, 10 cores, 16 GiB RAM, macOS 26.0 (build 25A8353), Darwin 25.0.0 arm64 |
| Python | 3.13.11, benchmark module from this repository |
| PostgreSQL | 17.11 (Debian 17.11-1.pgdg13+2), `postgres:17`, container `engineering-beyond-assumptions-db-1` |
| Placement | Containerized. Colima 0.9.1 VM: 2 CPUs, 2,054,565,888 bytes RAM, Docker server 28.4.0, overlayfs. The client on the Mac used `127.0.0.1:5434`, forwarded to port 5432 in the container |
| Database | `experiments`, Alembic revision `001`. `counters.name` is `varchar(100)` primary key; `counters.value` is `bigint NOT NULL` |
| Server settings | `shared_buffers=128MB`, `work_mem=4MB`, `maintenance_work_mem=64MB`, `effective_cache_size=4GB`, `max_connections=100`, `synchronous_commit=on`, `fsync=on`, `full_page_writes=on`, `wal_level=replica`, `max_wal_size=1GB`, `checkpoint_timeout=5min`, `deadlock_timeout=1s`, `log_lock_waits=off`, `lock_timeout=0`, `statement_timeout=0`, `default_transaction_isolation=read committed`, `track_io_timing=off`, `shared_preload_libraries` empty. Installed extension: `plpgsql` only |
| Other load | The Compose app container was stopped. Another local PostgreSQL already held host port 5432, so this database was published on host port 5434. No other client used this database during the matrix |

### Commands

```sh
export DATABASE_URL='postgresql+psycopg://experiments:experiments@localhost:5434/experiments'
alembic upgrade head
pytest -m postgres
# then, for repetition in 1 2; workers in 1 2 4 8 16;
# strategy in atomic read-modify-write;
# isolation in 'READ COMMITTED' 'REPEATABLE READ' 'SERIALIZABLE':
python -m experiments.experiment_001.benchmark \
  --workers "$workers" --iterations 100 \
  --strategy "$strategy" --isolation "$isolation"
```

Schema check after `alembic upgrade head` showed revision `001` and the two columns and primary key above. `pytest -m postgres` passed all 3 selected tests. The lost-update test was then repeated 30 times and passed 30/30: two READ COMMITTED transactions read 0, waited on a barrier, and both wrote 1, leaving `value = 1`. `test_atomic_concurrent_writes` passed with 8 workers, 25 iterations, atomic updates, and READ COMMITTED: 0 failures and final value 200.

### Results

`deadlocks` was 0 in all 60 runs. Every counted failure had SQLSTATE `40001` (`serialization_failure`). During the matrix window the database log contained 18,779 lines of `could not serialize access due to concurrent update` and no other `ERROR`. That count equals the sum of `failed` across the 60 JSON files. Lost updates are `committed - final_value`. Where lost updates are 0, the final value equals the number of commits.

Atomic updates at READ COMMITTED committed every attempt. Final value was 100, 200, 400, 800, and 1600 for 1, 2, 4, 8, and 16 workers.

| workers | rep | committed | failed | lost | commits/s | p50 ms | p95 ms | p99 ms | elapsed s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 100 | 0 | 0 | 1109.3 | 0.90 | 1.00 | 1.15 | 0.090 |
| 1 | 2 | 100 | 0 | 0 | 1233.0 | 0.78 | 0.84 | 1.13 | 0.081 |
| 2 | 1 | 200 | 0 | 0 | 2687.2 | 0.66 | 0.89 | 1.11 | 0.074 |
| 2 | 2 | 200 | 0 | 0 | 2670.4 | 0.67 | 0.88 | 1.06 | 0.075 |
| 4 | 1 | 400 | 0 | 0 | 3054.7 | 1.16 | 1.91 | 2.64 | 0.131 |
| 4 | 2 | 400 | 0 | 0 | 2898.9 | 1.22 | 1.89 | 2.32 | 0.138 |
| 8 | 1 | 800 | 0 | 0 | 2988.2 | 2.00 | 5.50 | 7.65 | 0.268 |
| 8 | 2 | 800 | 0 | 0 | 2921.5 | 1.91 | 5.21 | 7.81 | 0.274 |
| 16 | 1 | 1600 | 0 | 0 | 2710.7 | 3.66 | 14.78 | 22.35 | 0.590 |
| 16 | 2 | 1600 | 0 | 0 | 2697.2 | 3.82 | 14.61 | 22.00 | 0.593 |

Read–modify–write at READ COMMITTED also committed every attempt. With one worker the final value stayed 100. With two or more workers the final value fell behind the commit count.

| workers | rep | committed | failed | lost | final | commits/s | p50 ms | p95 ms | p99 ms | elapsed s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 100 | 0 | 0 | 100 | 843.1 | 1.19 | 1.48 | 1.88 | 0.119 |
| 1 | 2 | 100 | 0 | 0 | 100 | 878.7 | 1.11 | 1.33 | 1.42 | 0.114 |
| 2 | 1 | 200 | 0 | 99 | 101 | 2010.1 | 0.90 | 1.17 | 1.71 | 0.099 |
| 2 | 2 | 200 | 0 | 96 | 104 | 1565.4 | 1.11 | 1.87 | 2.44 | 0.128 |
| 4 | 1 | 400 | 0 | 269 | 131 | 2776.9 | 1.32 | 1.93 | 2.23 | 0.144 |
| 4 | 2 | 400 | 0 | 267 | 133 | 2515.3 | 1.42 | 2.18 | 2.89 | 0.159 |
| 8 | 1 | 800 | 0 | 670 | 130 | 2591.7 | 2.14 | 6.27 | 9.49 | 0.309 |
| 8 | 2 | 800 | 0 | 663 | 137 | 2592.0 | 2.24 | 6.33 | 9.05 | 0.309 |
| 16 | 1 | 1600 | 0 | 1490 | 110 | 2502.2 | 3.93 | 17.18 | 25.64 | 0.639 |
| 16 | 2 | 1600 | 0 | 1469 | 131 | 2316.1 | 4.28 | 17.29 | 26.14 | 0.691 |

At REPEATABLE READ and SERIALIZABLE, both strategies kept `lost = 0` and `final = committed`. Extra workers mostly added `40001` failures because the benchmark does not retry. One worker had no failures at either isolation.

Atomic updates at REPEATABLE READ:

| workers | rep | committed | failed | lost | commits/s | p50 ms | p95 ms | p99 ms | elapsed s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 100 | 0 | 0 | 1132.3 | 0.88 | 0.96 | 1.13 | 0.088 |
| 1 | 2 | 100 | 0 | 0 | 1119.7 | 0.90 | 0.97 | 1.02 | 0.089 |
| 2 | 1 | 149 | 51 | 0 | 1708.9 | 0.81 | 1.05 | 1.19 | 0.087 |
| 2 | 2 | 139 | 61 | 0 | 1537.1 | 0.85 | 1.08 | 1.17 | 0.090 |
| 4 | 1 | 172 | 228 | 0 | 1555.3 | 0.98 | 1.36 | 1.90 | 0.111 |
| 4 | 2 | 172 | 228 | 0 | 1474.3 | 1.02 | 1.54 | 2.16 | 0.117 |
| 8 | 1 | 196 | 604 | 0 | 1146.5 | 1.45 | 2.02 | 2.53 | 0.171 |
| 8 | 2 | 197 | 603 | 0 | 1118.0 | 1.47 | 2.08 | 2.77 | 0.176 |
| 16 | 1 | 237 | 1363 | 0 | 654.4 | 3.01 | 4.47 | 6.82 | 0.362 |
| 16 | 2 | 254 | 1346 | 0 | 647.7 | 3.27 | 5.03 | 6.11 | 0.392 |

Atomic updates at SERIALIZABLE:

| workers | rep | committed | failed | lost | commits/s | p50 ms | p95 ms | p99 ms | elapsed s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 100 | 0 | 0 | 1121.4 | 0.89 | 0.98 | 1.15 | 0.089 |
| 1 | 2 | 100 | 0 | 0 | 1167.8 | 0.87 | 1.12 | 1.24 | 0.086 |
| 2 | 1 | 135 | 65 | 0 | 1505.0 | 0.83 | 1.08 | 1.36 | 0.090 |
| 2 | 2 | 136 | 64 | 0 | 1468.2 | 0.85 | 1.09 | 1.22 | 0.093 |
| 4 | 1 | 161 | 239 | 0 | 1448.2 | 0.99 | 1.38 | 1.77 | 0.111 |
| 4 | 2 | 159 | 241 | 0 | 1372.9 | 1.02 | 1.45 | 1.87 | 0.116 |
| 8 | 1 | 198 | 602 | 0 | 1130.5 | 1.48 | 2.00 | 2.68 | 0.175 |
| 8 | 2 | 199 | 601 | 0 | 1098.3 | 1.50 | 2.51 | 3.32 | 0.181 |
| 16 | 1 | 253 | 1347 | 0 | 700.5 | 3.06 | 4.49 | 5.82 | 0.361 |
| 16 | 2 | 254 | 1346 | 0 | 696.8 | 3.07 | 4.50 | 5.27 | 0.365 |

Read–modify–write at REPEATABLE READ:

| workers | rep | committed | failed | lost | commits/s | p50 ms | p95 ms | p99 ms | elapsed s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 100 | 0 | 0 | 845.3 | 1.19 | 1.30 | 1.43 | 0.118 |
| 1 | 2 | 100 | 0 | 0 | 846.7 | 1.19 | 1.28 | 1.52 | 0.118 |
| 2 | 1 | 101 | 99 | 0 | 825.0 | 1.10 | 1.90 | 2.01 | 0.122 |
| 2 | 2 | 101 | 99 | 0 | 814.2 | 1.13 | 1.98 | 2.08 | 0.124 |
| 4 | 1 | 113 | 287 | 0 | 606.0 | 1.74 | 2.69 | 3.23 | 0.186 |
| 4 | 2 | 110 | 290 | 0 | 719.5 | 1.36 | 2.04 | 2.46 | 0.153 |
| 8 | 1 | 148 | 652 | 0 | 641.8 | 1.98 | 2.68 | 3.04 | 0.231 |
| 8 | 2 | 146 | 654 | 0 | 614.8 | 2.05 | 3.17 | 3.78 | 0.237 |
| 16 | 1 | 186 | 1414 | 0 | 363.2 | 4.55 | 6.34 | 7.28 | 0.512 |
| 16 | 2 | 198 | 1402 | 0 | 367.7 | 4.67 | 6.96 | 9.18 | 0.538 |

Read–modify–write at SERIALIZABLE:

| workers | rep | committed | failed | lost | commits/s | p50 ms | p95 ms | p99 ms | elapsed s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 100 | 0 | 0 | 855.1 | 1.16 | 1.30 | 1.44 | 0.117 |
| 1 | 2 | 100 | 0 | 0 | 862.5 | 1.16 | 1.27 | 1.46 | 0.116 |
| 2 | 1 | 101 | 99 | 0 | 810.2 | 1.13 | 1.92 | 2.06 | 0.125 |
| 2 | 2 | 100 | 100 | 0 | 784.1 | 1.15 | 1.96 | 2.21 | 0.128 |
| 4 | 1 | 116 | 284 | 0 | 807.7 | 1.32 | 1.68 | 2.15 | 0.144 |
| 4 | 2 | 106 | 294 | 0 | 720.6 | 1.33 | 1.94 | 2.35 | 0.147 |
| 8 | 1 | 151 | 649 | 0 | 672.8 | 1.96 | 2.63 | 3.29 | 0.224 |
| 8 | 2 | 155 | 645 | 0 | 682.5 | 1.92 | 2.61 | 3.17 | 0.227 |
| 16 | 1 | 191 | 1409 | 0 | 359.8 | 4.75 | 6.70 | 7.74 | 0.531 |
| 16 | 2 | 187 | 1413 | 0 | 337.7 | 4.81 | 7.26 | 9.24 | 0.554 |

The two repetitions agree on which cells lose updates and which cells return `40001`. The lost-update counts and the commit rates still move between repetitions. For read–modify–write at READ COMMITTED with 16 workers, lost updates were 1490 and 1469. For atomic updates at SERIALIZABLE with 16 workers, commits were 253 and 254 out of 1600 attempts.

### Limitations of this run

The database had 2 CPUs and about 1.9 GiB inside Colima, while the client ran on the Mac and crossed the port forward. Latency therefore includes thread startup, the barrier, VM scheduling, and that hop. Sixteen workers oversubscribe those 2 CPUs. There was no warmup, no retry, and no think time. p99 is one order statistic, `sorted[floor((n - 1) * 0.99)]`, so with 100 samples it sits next to the maximum. Lock-wait time, lock-wait counts, and buffer I/O time were not available with the settings above; the only lock counter collected was `deadlocks`, which stayed 0. A duplicate-key error in the log at 01:26:54 UTC came from the earlier API uniqueness test, outside this matrix. One 16-worker serializable read–modify–write command was run once before the matrix to check the harness and is omitted here. These numbers describe this environment only.

## Phase 2

Phase 1 does not retry. Phase 2 adds a bounded retry around the whole transaction and compares one hot row with spread counters. The Phase 1 function, command, and recorded tables above stay as the no-retry baseline.

### Retry

`run_transaction` starts a new transaction and runs the strategy body from the beginning. It retries only SQLSTATE `40001`. Any other database error stops that logical operation. After each serialization failure except the last, it sleeps for a full-jitter delay `uniform(0, min(max_delay, base_delay * 2^retry_index))`, where `retry_index` starts at 0. The official budget is `max_retries=5`, `base_delay=0.001` seconds, and `max_delay=0.05` seconds. `max_retries=0` is the Phase 1 behavior: a `40001` is reported as an exhausted logical operation and is not retried.

Reported counts, all excluding warmup:

| Name | Meaning |
|---|---|
| logical operations | Increments requested during the measurement window |
| database attempts | Transactions started, including the first try and retries |
| committed | Logical operations whose transaction committed |
| retries | Attempts after the first try (`database_attempts - logical_operations`) |
| exhausted retries | Logical operations that ended on `40001` after the budget was spent |

`lost_updates` is committed logical operations minus the sum of counter increases during the window. Latency percentiles use successful logical operations only and include backoff inside those operations. p50 is `statistics.median`. p95 and p99 use `sorted[floor((n - 1) * q)]`. p99 is omitted below 100 successful samples. Throughput is committed logical operations divided by the measurement-window duration.

### Workload

Each cell uses 8 workers, 50 warmup iterations per worker, and 500 measured iterations per worker. Warmup uses the same strategy and retry budget and is excluded from the totals. A barrier action reads every counter after warmup and before any measured increment. The target row is `names[(worker_index + step) % counters]`, so one counter is a hot row and 10 or 100 counters spread the same number of operations. Both `atomic` and `read-modify-write` run at READ COMMITTED, REPEATABLE READ, and SERIALIZABLE, with `max_retries` 0 and 5. Each cell is repeated 3 times. The client samples `pg_stat_activity` lock waits and ungranted `pg_locks` every 0.05 seconds during the window. Samples are counts, not wait time.

```sh
export DATABASE_URL='postgresql+psycopg://experiments:experiments@localhost:5434/experiments'
python -c 'from sqlalchemy import create_engine; from app.config import Settings; from experiments.experiment_001.phase2 import set_log_lock_waits; e=create_engine(Settings().database_url); set_log_lock_waits(e, True); e.dispose()'
python -m experiments.experiment_001.phase2 \
  --workers 8 --warmup-iterations 50 --iterations 500 \
  --counters 1 --strategy atomic --isolation 'READ COMMITTED' \
  --max-retries 0 --base-delay-seconds 0.001 --max-delay-seconds 0.05 \
  --sample-interval-seconds 0.05
```

`log_lock_waits` is enabled for the matrix and reset afterward. PostgreSQL only logs a lock wait that lasts at least `deadlock_timeout` (1s). The setting does not change the SQL.

### Recorded run

Raw JSON is in `benchmark-results/experiment_001_phase2/`, which is gitignored. The Phase 1 files in `benchmark-results/experiment_001/` were left in place (60 files). Throughput is rounded to 0.1 operations/s and latency to 0.01 ms. Counts are exact. Every cell had 4,000 logical operations. Every successful sample was large enough to report p99. `other_failures` was 0 in every file. Every measurement-window database error was SQLSTATE `40001`. `deadlocks` was 0. The app container was stopped. `log_lock_waits` was `on` during the run and was reset to `off` afterward. `deadlock_timeout` stayed `1s`.

| Item | Value |
|---|---|
| When (UTC) | 2026-11-24 01:40:40 through 01:43:02 |
| Client and server | Same machine, Colima VM, PostgreSQL 17.11, and host port 5434 as the Phase 1 run |
| Workload | 8 workers, 50 warmup iterations, 500 measured iterations, counters 1/10/100, both strategies, three isolation levels, `max_retries` 0 and 5, 3 repetitions (108 cells) |
| Retry | Full jitter, base 0.001 s, cap 0.05 s |
| Lock sample | Every 0.05 s: sessions in `pg_stat_activity` with `wait_event_type = 'Lock'`, and server-wide rows in `pg_locks` with `granted = false` |

In the matrix window the database log contained 148,575 lines of `could not serialize access due to concurrent update` and 599 lines of `could not serialize access due to read/write dependencies among transactions`. Both are `40001`. The measurement JSON counts 136,295 failed attempts. The log is larger because warmup uses the same statements and its errors are excluded from the JSON. The log contained no `still waiting for` lock-wait lines. Sampled peaks are in the tables; they are not durations.

Atomic updates at READ COMMITTED committed all 4,000 operations, lost none, and retried none. Spreading the rows raised throughput and cut the tail. One counter stayed near 3,100–3,200 operations/s with p99 around 8–9 ms. Ten and 100 counters were about 5,000–6,000 operations/s with p99 around 2–4 ms.

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 4000 | 0 | 0 | 0 | 3170.5 | 2.01 | 5.64 | 8.33 | 6 | 6 |
| 1 | 2 | 4000 | 0 | 0 | 0 | 3142.5 | 2.06 | 5.64 | 8.49 | 6 | 6 |
| 1 | 3 | 4000 | 0 | 0 | 0 | 3209.8 | 1.99 | 5.52 | 7.89 | 6 | 7 |
| 10 | 1 | 4000 | 0 | 0 | 0 | 5986.1 | 1.30 | 1.72 | 1.90 | 1 | 1 |
| 10 | 2 | 4000 | 0 | 0 | 0 | 5999.6 | 1.30 | 1.68 | 1.89 | 1 | 1 |
| 10 | 3 | 4000 | 0 | 0 | 0 | 5681.9 | 1.36 | 1.88 | 2.29 | 1 | 1 |
| 100 | 1 | 4000 | 0 | 0 | 0 | 5774.1 | 1.32 | 1.88 | 2.58 | 1 | 0 |
| 100 | 2 | 4000 | 0 | 0 | 0 | 5741.9 | 1.34 | 1.84 | 2.22 | 2 | 1 |
| 100 | 3 | 4000 | 0 | 0 | 0 | 5028.9 | 1.46 | 2.29 | 4.17 | 1 | 0 |

The same atomic READ COMMITTED cells with `max_retries=5` also retried zero times. The budget did not change this isolation level.

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 4000 | 0 | 0 | 0 | 3107.0 | 2.01 | 5.93 | 9.03 | 7 | 7 |
| 1 | 2 | 4000 | 0 | 0 | 0 | 3206.1 | 1.97 | 5.64 | 7.94 | 7 | 7 |
| 1 | 3 | 4000 | 0 | 0 | 0 | 3208.4 | 2.00 | 5.45 | 8.40 | 6 | 6 |
| 10 | 1 | 4000 | 0 | 0 | 0 | 5965.9 | 1.31 | 1.69 | 1.90 | 1 | 1 |
| 10 | 2 | 4000 | 0 | 0 | 0 | 5937.5 | 1.31 | 1.73 | 2.00 | 1 | 1 |
| 10 | 3 | 4000 | 0 | 0 | 0 | 5787.7 | 1.35 | 1.79 | 2.05 | 1 | 1 |
| 100 | 1 | 4000 | 0 | 0 | 0 | 5906.1 | 1.33 | 1.73 | 1.93 | 1 | 1 |
| 100 | 2 | 4000 | 0 | 0 | 0 | 6016.4 | 1.29 | 1.66 | 1.87 | 1 | 1 |
| 100 | 3 | 4000 | 0 | 0 | 0 | 5523.6 | 1.39 | 1.96 | 2.63 | 2 | 1 |

Read–modify–write at READ COMMITTED committed every operation and retried none. Across both retry budgets, the hot row lost 3,390–3,428 updates, ten counters lost 903–1,124, and one hundred counters lost 378–684.

`max_retries=0`:

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 4000 | 0 | 0 | 3420 | 2870.3 | 2.23 | 6.28 | 9.19 | 6 | 6 |
| 1 | 2 | 4000 | 0 | 0 | 3393 | 2813.1 | 2.24 | 6.50 | 9.78 | 6 | 6 |
| 1 | 3 | 4000 | 0 | 0 | 3390 | 2851.0 | 2.25 | 6.29 | 9.11 | 6 | 7 |
| 10 | 1 | 4000 | 0 | 0 | 943 | 4351.4 | 1.81 | 2.26 | 2.49 | 1 | 1 |
| 10 | 2 | 4000 | 0 | 0 | 1041 | 4335.3 | 1.80 | 2.30 | 2.74 | 1 | 1 |
| 10 | 3 | 4000 | 0 | 0 | 1124 | 4147.7 | 1.87 | 2.51 | 2.95 | 1 | 1 |
| 100 | 1 | 4000 | 0 | 0 | 378 | 4386.9 | 1.78 | 2.22 | 2.43 | 0 | 0 |
| 100 | 2 | 4000 | 0 | 0 | 640 | 4237.7 | 1.81 | 2.45 | 2.91 | 1 | 1 |
| 100 | 3 | 4000 | 0 | 0 | 551 | 4078.2 | 1.90 | 2.47 | 2.76 | 0 | 1 |

`max_retries=5`:

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 4000 | 0 | 0 | 3422 | 2806.3 | 2.23 | 6.43 | 9.58 | 6 | 6 |
| 1 | 2 | 4000 | 0 | 0 | 3428 | 2875.7 | 2.16 | 6.30 | 9.39 | 6 | 6 |
| 1 | 3 | 4000 | 0 | 0 | 3402 | 2849.1 | 2.19 | 6.31 | 8.97 | 6 | 6 |
| 10 | 1 | 4000 | 0 | 0 | 951 | 4380.7 | 1.79 | 2.25 | 2.54 | 1 | 1 |
| 10 | 2 | 4000 | 0 | 0 | 982 | 4312.5 | 1.81 | 2.34 | 2.71 | 1 | 1 |
| 10 | 3 | 4000 | 0 | 0 | 903 | 4243.7 | 1.83 | 2.42 | 2.84 | 2 | 1 |
| 100 | 1 | 4000 | 0 | 0 | 499 | 4205.9 | 1.83 | 2.44 | 2.91 | 1 | 0 |
| 100 | 2 | 4000 | 0 | 0 | 592 | 4383.5 | 1.78 | 2.23 | 2.44 | 1 | 1 |
| 100 | 3 | 4000 | 0 | 0 | 684 | 3945.4 | 1.91 | 2.83 | 4.11 | 1 | 1 |

At REPEATABLE READ and SERIALIZABLE, lost updates stayed 0 for both strategies. Without retries the hot row committed roughly 990–1,012 atomic operations and 718–743 read–modify–write operations out of 4,000. The rest were exhausted `40001`s. Ten and 100 counters exhausted fewer operations, and 100 counters still exhausted hundreds when retries were off.

Atomic, REPEATABLE READ, `max_retries=0`:

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 990 | 0 | 3010 | 0 | 1266.1 | 1.31 | 1.75 | 1.96 | 5 | 6 |
| 1 | 2 | 993 | 0 | 3007 | 0 | 1269.9 | 1.30 | 1.73 | 2.22 | 4 | 4 |
| 1 | 3 | 1012 | 0 | 2988 | 0 | 1302.2 | 1.29 | 1.73 | 1.91 | 7 | 3 |
| 10 | 1 | 3278 | 0 | 722 | 0 | 4649.0 | 1.34 | 1.78 | 2.04 | 1 | 2 |
| 10 | 2 | 3265 | 0 | 735 | 0 | 4425.4 | 1.39 | 1.95 | 2.35 | 1 | 2 |
| 10 | 3 | 3303 | 0 | 697 | 0 | 4600.7 | 1.37 | 1.84 | 2.11 | 1 | 2 |
| 100 | 1 | 3584 | 0 | 416 | 0 | 5187.8 | 1.33 | 1.73 | 1.91 | 2 | 1 |
| 100 | 2 | 3844 | 0 | 156 | 0 | 5628.7 | 1.32 | 1.72 | 1.92 | 1 | 0 |
| 100 | 3 | 3814 | 0 | 186 | 0 | 5138.3 | 1.38 | 1.92 | 2.62 | 1 | 1 |

Atomic, REPEATABLE READ, `max_retries=5`. The hot row committed 3,742–3,774 and still exhausted 226–258. Ten counters exhausted 0, 0, and 2. One hundred counters committed all 4,000, with 29–41 retries.

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 3751 | 4657 | 249 | 0 | 1530.8 | 1.15 | 13.25 | 24.79 | 2 | 3 |
| 1 | 2 | 3756 | 4723 | 244 | 0 | 1545.4 | 1.19 | 14.06 | 24.48 | 4 | 4 |
| 1 | 3 | 3774 | 4656 | 226 | 0 | 1546.4 | 1.15 | 13.71 | 26.13 | 5 | 3 |
| 10 | 1 | 4000 | 304 | 0 | 0 | 5004.3 | 1.34 | 3.06 | 6.44 | 1 | 1 |
| 10 | 2 | 4000 | 281 | 0 | 0 | 5235.7 | 1.29 | 2.00 | 6.30 | 1 | 1 |
| 10 | 3 | 3998 | 325 | 2 | 0 | 4087.4 | 1.40 | 4.52 | 10.06 | 0 | 1 |
| 100 | 1 | 4000 | 41 | 0 | 0 | 5905.1 | 1.29 | 1.69 | 2.15 | 0 | 0 |
| 100 | 2 | 4000 | 33 | 0 | 0 | 5898.7 | 1.30 | 1.68 | 2.03 | 0 | 0 |
| 100 | 3 | 4000 | 29 | 0 | 0 | 5542.9 | 1.34 | 1.94 | 3.00 | 0 | 0 |

Atomic, SERIALIZABLE, `max_retries=0`:

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 991 | 0 | 3009 | 0 | 1229.4 | 1.33 | 1.85 | 2.48 | 5 | 5 |
| 1 | 2 | 995 | 0 | 3005 | 0 | 1274.0 | 1.32 | 1.76 | 2.05 | 4 | 4 |
| 1 | 3 | 999 | 0 | 3001 | 0 | 1297.5 | 1.29 | 1.71 | 2.02 | 4 | 4 |
| 10 | 1 | 3332 | 0 | 668 | 0 | 4742.3 | 1.35 | 1.79 | 2.07 | 2 | 1 |
| 10 | 2 | 3088 | 0 | 912 | 0 | 4304.1 | 1.36 | 1.82 | 2.15 | 2 | 2 |
| 10 | 3 | 3410 | 0 | 590 | 0 | 4740.2 | 1.37 | 1.87 | 2.28 | 1 | 1 |
| 100 | 1 | 3594 | 0 | 406 | 0 | 5214.2 | 1.33 | 1.74 | 1.96 | 1 | 1 |
| 100 | 2 | 3670 | 0 | 330 | 0 | 5007.9 | 1.37 | 1.93 | 2.60 | 1 | 1 |
| 100 | 3 | 3826 | 0 | 174 | 0 | 5291.9 | 1.34 | 1.86 | 3.66 | 0 | 0 |

Atomic, SERIALIZABLE, `max_retries=5`. The hot row still exhausted 241–258 logical operations after about 4,600–4,700 retries. p99 of the operations that did commit was about 25–26 ms, which includes backoff.

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 3756 | 4613 | 244 | 0 | 1554.6 | 1.16 | 12.02 | 25.77 | 7 | 4 |
| 1 | 2 | 3759 | 4674 | 241 | 0 | 1536.8 | 1.14 | 14.26 | 26.13 | 4 | 6 |
| 1 | 3 | 3742 | 4697 | 258 | 0 | 1535.9 | 1.15 | 13.31 | 25.23 | 4 | 3 |
| 10 | 1 | 3997 | 289 | 3 | 0 | 5229.0 | 1.28 | 2.03 | 5.98 | 0 | 1 |
| 10 | 2 | 4000 | 276 | 0 | 0 | 5275.5 | 1.31 | 2.93 | 5.60 | 0 | 0 |
| 10 | 3 | 4000 | 314 | 0 | 0 | 4982.4 | 1.35 | 3.00 | 6.54 | 0 | 1 |
| 100 | 1 | 4000 | 63 | 0 | 0 | 5706.2 | 1.32 | 1.78 | 3.15 | 0 | 1 |
| 100 | 2 | 4000 | 65 | 0 | 0 | 5825.8 | 1.30 | 1.74 | 3.29 | 0 | 0 |
| 100 | 3 | 4000 | 60 | 0 | 0 | 5182.9 | 1.39 | 2.27 | 4.43 | 1 | 0 |

Read–modify–write, REPEATABLE READ, `max_retries=0`:

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 718 | 0 | 3282 | 0 | 694.2 | 1.87 | 2.38 | 2.66 | 1 | 3 |
| 1 | 2 | 738 | 0 | 3262 | 0 | 718.8 | 1.86 | 2.43 | 2.95 | 4 | 5 |
| 1 | 3 | 743 | 0 | 3257 | 0 | 734.2 | 1.83 | 2.27 | 2.49 | 2 | 5 |
| 10 | 1 | 2466 | 0 | 1534 | 0 | 2518.4 | 1.86 | 2.45 | 2.88 | 2 | 1 |
| 10 | 2 | 2062 | 0 | 1938 | 0 | 2079.2 | 1.88 | 2.46 | 2.76 | 2 | 2 |
| 10 | 3 | 2580 | 0 | 1420 | 0 | 2574.5 | 1.90 | 2.51 | 2.94 | 1 | 1 |
| 100 | 1 | 3481 | 0 | 519 | 0 | 3706.2 | 1.80 | 2.31 | 2.60 | 1 | 1 |
| 100 | 2 | 3463 | 0 | 537 | 0 | 3615.6 | 1.85 | 2.42 | 2.78 | 1 | 1 |
| 100 | 3 | 3663 | 0 | 337 | 0 | 3491.1 | 1.98 | 2.87 | 3.56 | 1 | 0 |

Read–modify–write, REPEATABLE READ, `max_retries=5`. One counter exhausted 439–457 operations. Ten counters exhausted one operation in each repetition. One hundred counters committed all 4,000.

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 3543 | 6825 | 457 | 0 | 899.6 | 3.19 | 18.07 | 29.26 | 2 | 4 |
| 1 | 2 | 3561 | 7079 | 439 | 0 | 895.7 | 3.38 | 18.04 | 28.67 | 4 | 4 |
| 1 | 3 | 3555 | 6931 | 445 | 0 | 891.5 | 3.22 | 18.59 | 30.94 | 3 | 2 |
| 10 | 1 | 3999 | 406 | 1 | 0 | 3508.1 | 1.83 | 4.49 | 8.82 | 0 | 1 |
| 10 | 2 | 3999 | 455 | 1 | 0 | 3601.3 | 1.81 | 4.52 | 9.49 | 2 | 0 |
| 10 | 3 | 3999 | 417 | 1 | 0 | 3389.7 | 1.92 | 4.69 | 9.99 | 1 | 1 |
| 100 | 1 | 4000 | 54 | 0 | 0 | 4204.8 | 1.80 | 2.34 | 4.40 | 0 | 0 |
| 100 | 2 | 4000 | 37 | 0 | 0 | 4262.2 | 1.80 | 2.29 | 2.85 | 0 | 0 |
| 100 | 3 | 4000 | 30 | 0 | 0 | 3977.1 | 1.91 | 2.59 | 3.04 | 0 | 0 |

Read–modify–write, SERIALIZABLE, `max_retries=0`:

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 727 | 0 | 3273 | 0 | 723.3 | 1.82 | 2.35 | 2.64 | 6 | 4 |
| 1 | 2 | 722 | 0 | 3278 | 0 | 710.6 | 1.83 | 2.31 | 2.71 | 4 | 5 |
| 1 | 3 | 722 | 0 | 3278 | 0 | 694.3 | 1.86 | 2.49 | 3.14 | 2 | 6 |
| 10 | 1 | 2670 | 0 | 1330 | 0 | 2765.4 | 1.87 | 2.39 | 2.81 | 1 | 1 |
| 10 | 2 | 2412 | 0 | 1588 | 0 | 2382.0 | 1.93 | 2.53 | 2.90 | 3 | 1 |
| 10 | 3 | 2268 | 0 | 1732 | 0 | 2058.0 | 2.05 | 2.86 | 3.43 | 2 | 3 |
| 100 | 1 | 3080 | 0 | 920 | 0 | 3192.0 | 1.85 | 2.34 | 2.70 | 0 | 1 |
| 100 | 2 | 3312 | 0 | 688 | 0 | 3453.5 | 1.85 | 2.39 | 2.68 | 1 | 1 |
| 100 | 3 | 3055 | 0 | 945 | 0 | 2953.6 | 1.94 | 2.66 | 4.32 | 0 | 1 |

Read–modify–write, SERIALIZABLE, `max_retries=5`:

| counters | rep | committed | retries | exhausted | lost | ops/s | p50 ms | p95 ms | p99 ms | max lock waits | max ungranted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 3549 | 7042 | 451 | 0 | 889.3 | 3.30 | 18.41 | 30.60 | 4 | 4 |
| 1 | 2 | 3549 | 7035 | 451 | 0 | 898.1 | 3.25 | 18.58 | 31.63 | 2 | 4 |
| 1 | 3 | 3557 | 6968 | 443 | 0 | 901.0 | 3.24 | 18.38 | 28.74 | 3 | 6 |
| 10 | 1 | 3997 | 449 | 3 | 0 | 3615.1 | 1.80 | 4.46 | 8.44 | 1 | 0 |
| 10 | 2 | 4000 | 465 | 0 | 0 | 3629.5 | 1.79 | 4.54 | 9.47 | 1 | 1 |
| 10 | 3 | 3999 | 396 | 1 | 0 | 3626.2 | 1.83 | 4.25 | 8.46 | 1 | 0 |
| 100 | 1 | 4000 | 82 | 0 | 0 | 4214.2 | 1.79 | 2.30 | 4.36 | 0 | 0 |
| 100 | 2 | 4000 | 139 | 0 | 0 | 4130.2 | 1.80 | 2.40 | 4.65 | 0 | 0 |
| 100 | 3 | 4000 | 95 | 0 | 0 | 4064.5 | 1.84 | 2.44 | 4.73 | 0 | 0 |

Sampled lock waits peaked at 6 or 7 sessions on the single counter and at 0–3 sessions when the same 4,000 operations were spread across 10 or 100 counters. Ungranted `pg_locks` followed the same pattern. Those peaks are the maximum of 0.05-second samples, so a shorter wait can be missed and a single sample is not a wait duration.

### Limitations of the phase 2 run

The client still crosses the Colima port forward onto a 2-CPU VM, and 8 workers oversubscribe those CPUs. Latency of a successful logical operation includes its retries and backoff; failed logical operations are excluded from the percentiles and remain visible as exhausted retries. p99 is one order statistic. The retry cap of 5 was chosen before the run and was not enough to commit every hot-row operation at REPEATABLE READ or SERIALIZABLE. Spreading with `(worker_index + step) % counters` does not assign a private counter to each worker, so 100 counters still overlap. Warmup errors are in the server log and are omitted from the tables. Lock-wait logging recorded nothing because observed waits were shorter than `deadlock_timeout`. There is no `pg_stat_statements` and `track_io_timing` is off, so lock-wait time was not measured. One serializable read–modify–write hot-row command with retries was run once to estimate duration and is not one of the three repetitions. These numbers describe this environment only.
