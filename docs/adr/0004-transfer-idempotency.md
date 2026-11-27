# ADR 0004: Transactional idempotency for laboratory transfers

Status: Accepted

## Context

A successful `POST /transfers` commits money movement before the HTTP response is
delivered. If the response is lost, a client that retries the same logical
operation without coordination can create a second transfer. Each transfer can
still satisfy ledger and balance invariants. The failure is at business-operation
identity, not inside a single double-entry posting.

Experiment 003 requires a laboratory contract that makes duplicate effects
measurable and prevents them on the protected path.

## Decision

1. **Required header.** `POST /transfers` requires `Idempotency-Key` (8–128
   characters: `A-Z`, `a-z`, `0-9`, `.`, `_`, `:`, `-`). Missing or malformed
   keys return `400`. There is no legacy unkeyed transfer path on the public API.

2. **Scope.** Keys live under scope `lab.transfer`. The API has no authentication
   or tenant model. Key uniqueness is laboratory-wide. Cross-customer key
   collisions are possible and are an accepted laboratory limitation until
   tenancy exists. Anyone who can call the laboratory API with another party’s
   key can also receive that party’s committed transfer on replay. This is a
   non-production information-exposure risk, not a multi-tenant design.

3. **Request fingerprint.** SHA-256 over a canonical JSON object with sorted
   keys: `operation`, `scope`, `source_account_id`, `destination_account_id`,
   `amount_minor`, `currency` (uppercased). Balances and timestamps are excluded.
   The digest identifies payload identity; it is not authentication and is not
   the sole operation identifier.

4. **Storage.** Table `idempotency_records` with a unique constraint on
   `(scope, idempotency_key)`, a partial unique index on `transfer_id`, and a
   check that `reserved` rows have no transfer while `completed` rows always
   reference one.

5. **Atomic reservation.** The protected path inserts a `reserved` row in a
   savepoint, runs `execute_transfer`, marks the record `completed`, and commits
   once. A uniqueness conflict loads the existing row with `FOR UPDATE` and
   either returns the original transfer (matching fingerprint) or raises
   `IdempotencyKeyConflict` (HTTP `409`).

6. **Business rejections.** Insufficient funds, currency mismatch, and unavailable
   accounts roll back the reservation. A later retry with the same key may
   reevaluate balances. Request-validation errors (`422` / malformed key) never
   create a reservation.

7. **Lock order.** Idempotency insert (unique index) first, then accounts in
   sorted UUID order, then ledger inserts. Concurrent duplicates block on the
   unique index until the winner commits; losers replay. No durable lone
   `processing` row is committed, so a crash mid-transaction does not permanently
   block retries.

8. **Fault injection.** Module hooks in `app.ledger.faults` are test-only. They
   are never enabled by public HTTP input.

## Consequences

- Sequential and concurrent equivalent requests produce at most one committed
  transfer for a given scope and key.
- Same key with a different fingerprint is rejected without further movement.
- Lost-response retries return HTTP `200` with `idempotent_replay=true`.
- Idempotency adds an insert and a unique-index coordination cost versus the
  Experiment 002 transfer path.
- Unsafe strategies under `experiments/experiment_003/` remain off the public
  API and may demonstrate duplicates deliberately.

## Alternatives rejected

- **Redis / external locks.** Adds a second durability domain for a PostgreSQL
  laboratory.
- **Payload fingerprint as primary key.** Collides distinct clients that send
  the same transfer shape and cannot express “same key, different payload.”
- **Durable `processing` without recovery.** Permanently blocks retries after a
  crash between reservation commit and transfer commit.
- **Python threading locks.** Do not coordinate across processes or guarantee
  database durability.
