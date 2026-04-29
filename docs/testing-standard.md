# MODOK Testing Standard

## Test Levels

Every EARS spec carries a test level annotation. Levels are cumulative.

| Level | Annotation | Requirement |
|---|---|---|
| Unit | `[U]` | At least one `@spec`-annotated test directly exercises the behavior with mocked dependencies. |
| Property | `[P]` | Invariant holds across arbitrary inputs via `hypothesis`. Implies `[U]`. |
| Contract | `[C]` | Verified against a live local Quine instance (Docker). Implies `[U]`. |

A spec marked `[P, C]` requires all three.

## Assignment Rules

- **[P]** is required when the spec contains "shall always", "shall never", or the trigger/input space is too large for exhaustive examples (ID uniqueness, idempotency, retry behavior, isolation guarantees).
- **[C]** is required when correctness depends on Quine's actual wire behavior, not just our model of it (node writes/reads, edge writes, traversal response shape, error codes).
- **[U]** is the minimum for all other specs.

## Tooling

- Unit and property tests: `pytest` + `hypothesis`
- Contract tests: `pytest` with a Docker Quine fixture (started in `conftest.py`, torn down after the session)
- HTTP mocking for unit tests: `httpx.MockTransport`
- `@spec` annotations: on the test function that directly exercises the spec, not on inner assertions

## What is not tested here

MODOK does not store full source files or run static analysis on ingested repos. Correctness of the current repo state is the agent's responsibility, not MODOK's. MODOK tests cover graph structure, retrieval correctness, and ingestion fidelity — not the accuracy of the underlying project knowledge.
