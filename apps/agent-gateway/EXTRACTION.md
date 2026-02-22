# Clean Separation Playbook

## Why this structure is clean

- No imports from the parent project codebase
- No dependence on KnowCode internals or files
- All coupling is network-level contract (`/openapi.json`, `/api/v1/*`)

## Repository split steps

1. Copy folder contents to a new repository root.
2. Rename project package if desired (optional).
3. Create CI:
   - `pytest tests`
   - `ruff check src tests`
4. Deploy with the same env variables.
5. Point `KNOWCODE_API_BASE_URL` to remote KnowCode service.

## Anti-patterns to avoid

- Importing `src/knowcode` directly.
- Reading parent repo files at runtime.
- Sharing secrets/config through relative files outside this app.
