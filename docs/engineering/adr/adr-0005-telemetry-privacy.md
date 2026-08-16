# ADR 0005: Telemetry privacy

**Status:** Accepted

**Date:** 2026-08-12

**Origin:** Parser, Index, and Security Hardening blueprint, Step 01 (archived source: docs/archive/hardening-contracts.md)

---


### Decision

Default telemetry is an allowlisted aggregate event schema. A query event may
contain classification, length bucket, routing result, sufficiency bucket,
duration, outcome, and a privacy-reviewed keyed correlation value. It does not
contain raw query text, retrieved code, prompt bodies, tokens, credentials, or
arbitrary caller-supplied nested fields.

The sink also applies recursive secret redaction and field/record length
bounds as defense in depth. Logs are local, created with mode `0600`, rotated,
retained for a bounded period, and deletable through a documented local
operation. One logical query produces one counted query event.

Raw query capture, if retained at all, is a separate explicit opt-in with a
warning, separate file, short retention, and tests. Redaction alone is not a
safe default.

