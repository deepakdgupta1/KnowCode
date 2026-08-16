# Architecture Decision Records

Numbered decisions with status. ADRs 1–7 were extracted from the hardening
blueprint's contracts document (Step 01, 2026-08-12); ADR 8 is the
persistence-format analysis.

| # | Title | Status | Date |
|---|---|---|---|
| [0001](adr-0001-entity-and-file-identity.md) | Entity and file identity | Accepted | 2026-08-12 |
| [0002](adr-0002-sqlite-connection-and-transaction-ownership.md) | SQLite connection and transaction ownership | Accepted | 2026-08-12 |
| [0003](adr-0003-durable-embedding-representation.md) | Durable embedding representation | Accepted | 2026-08-12 |
| [0004](adr-0004-complete-index-generations.md) | Complete index generations | Accepted | 2026-08-12 |
| [0005](adr-0005-telemetry-privacy.md) | Telemetry privacy | Accepted | 2026-08-12 |
| [0006](adr-0006-direct-server-proxy-trust.md) | Direct-server proxy trust | Accepted | 2026-08-12 |
| [0007](adr-0007-protocol-and-artifact-evolution-inventory.md) | Protocol and artifact evolution inventory | Accepted | 2026-08-12 |
| [0008](adr-0008-persistence-format-and-token-economics.md) | Persistence format and token economics | Proposed | 2026-03-07 |

## Conventions

- One decision per file, numbered in decision order, never reused.
- Status is one of *Proposed*, *Accepted*, *Superseded by N*.
- New ADRs are added, never edited in place; superseding is recorded by
  changing status and linking the successor.
