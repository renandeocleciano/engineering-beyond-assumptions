# ADR 0002: Increment counters in the database

Status: Accepted

Use `UPDATE counters SET value = value + 1 ... RETURNING` for the API increment. PostgreSQL serializes conflicting row updates, avoiding the lost update possible when an application reads a value and later writes an incremented value at READ COMMITTED. A hot row still creates contention. Stronger isolation can abort conflicting transactions; callers must handle failures and retry whole transactions where appropriate. The experiment compares these behaviors without silently retrying.
