# ADR 0002: SQLite connection and transaction ownership

**Status:** Accepted

**Date:** 2026-08-12

**Origin:** Parser, Index, and Security Hardening blueprint, Step 01 (archived source: docs/archive/hardening-contracts.md)

---


### Decision

Each worker or request execution context owns its SQLite connection. All
connections use WAL, `busy_timeout`, foreign-key enforcement, and the selected
durability setting. Unrelated threads never share a connection, including when
`check_same_thread=False` would permit it.

One repository-level writer coordinator serializes write transactions. A
public batch or replacement method owns its connection, lock, `BEGIN`, commit,
and rollback for the whole operation. Callers do not open an outer transaction
around individually locking repository methods. Reads use their context's
connection and observe committed snapshots only.

`close()` is idempotent. The bundle or service that creates a connection
factory owns it and closes connections only after active operation leases have
finished.

### Consequences

Step 08 adds transactional replacement; Step 09 changes connection ownership.
Tests must use barriers/events and the real shared-service topology. WAL alone
is not accepted as evidence of thread safety.

