# Experiment 003 benchmark evidence

Two versioned result sets are preserved in-repo (no credentials; laboratory numbers only).

| Directory | Schema | Notes |
| --- | --- | --- |
| `v1-initial/` | Initial 2026-11-27 run | Includes misleading field `unique_transfers_per_second` (= all committed transfer rows / elapsed, including duplicates and warmup). Kept for audit continuity. |
| `v2-publication/` | Schema version 2 | Renamed throughput fields, explicit `measurement` block for warmup vs measured, service-level (not HTTP) labeling. |

Both sets used PostgreSQL 17.11, Python 3.13.11, SQLAlchemy 2.1.9, `READ COMMITTED`, seed 42, single recorded pass per cell. Correctness conclusions match across versions; only metric naming and metadata differ.

Do not treat these figures as production capacity.
