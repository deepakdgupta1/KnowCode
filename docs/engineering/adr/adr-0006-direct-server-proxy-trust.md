# ADR 0006: Direct-server proxy trust

**Status:** Accepted

**Date:** 2026-08-12

**Origin:** Parser, Index, and Security Hardening blueprint, Step 01 (archived source: docs/archive/hardening-contracts.md)

---


### Decision

The normal local Uvicorn server explicitly disables proxy-header processing.
KnowCode does not currently support proxied deployment, so it exposes no
trusted-proxy option in this hardening series. `X-Forwarded-For` and
`Forwarded` are ordinary untrusted headers in direct mode.

Future proxy support requires a separate design with an explicit narrow IP or
CIDR allowlist. Wildcard trust is rejected by default. Rate-limit tests must
start the real server stack with the limiter enabled; changing SlowAPI's key
function without changing Uvicorn trust is not a fix.

