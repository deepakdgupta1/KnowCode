# Retrieval Quality Evaluation

KnowCode's effectiveness harness now lives in the independent
[`knowcode-evals`](https://github.com/deepakdgupta1/knowcode-evals) repository.
The split keeps benchmark data, judge orchestration, external downloads,
statistical gates, and generated evidence out of the product package.

KnowCode owns only the runtime contract:

1. Local answering is disabled unless a policy artifact is explicitly
   configured.
2. The artifact must use the supported schema and target an exact KnowCode
   version and Git revision.
3. The caller must provide the artifact's trusted SHA-256 separately.
4. KnowCode verifies the locked-holdout identity, external dataset identities,
   judge identities, metric floors, canary evidence, and cited source hashes.
5. Any missing, malformed, or drifted evidence fails closed to the LLM path.

For dataset maintenance, local evaluator setup, CI operation, and artifact
promotion, use the evaluator repository's `README.md` and
`docs/retrieval-evals.md`.

## Consuming a blessed policy

Obtain both files from the trusted evaluator CI run for the exact KnowCode
revision being deployed:

- `machine-verification.json`
- `machine-verification.sha256`

Configure the runtime with separate values:

```bash
export KNOWCODE_ROUTING_POLICY_ARTIFACT=/secure/path/machine-verification.json
export KNOWCODE_ROUTING_POLICY_SHA256=<trusted-sha256>
```

Do not compute the trusted digest from an artifact received through an
untrusted channel. The digest is the independent authenticity pin.

## Current status

The consumer boundary and fail-closed enforcement are implemented. P1 is not
yet complete: the existing 60-case corpus is calibration-only, and no locked
holdout plus full external run has produced a blessed policy. The default local
task allowlist therefore remains empty.
