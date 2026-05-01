# Ingestion Pipeline LLD — Pre-Draft Notes

Notes captured before the LLD is written. Address these in Phase 2 (LLD) for component 2.

## Ingestion trigger model

Decide whether ingest runs are:
- **Manual** — developer runs `modok ingest-docs` after a commit (or pre/post-commit hook)
- **Automatic** — git hook or CI step runs ingestion on every commit touching relevant files

The stagehand use case (tracking issues against specific code changes) likely wants automatic or hook-driven ingestion.

## Commit SHA on nodes

`Fix` and `ResolutionEvent` nodes should carry the commit SHA of the change that introduced the fix. This lets MODOK answer: "which commit resolved this issue?" and "which commit is implicated in this regression?"

Evaluate which other node types benefit from a `commit_sha` property (e.g., `KnownIssue` discovery, `ObservationEvent` correlation with a deploy).

## Write-on-commit intent

The user's intent is: every commit to the codebase that touches relevant files should result in a graph update. The Quine client LLD supports this — `upsert_node` and `write_edge` are idempotent so repeated ingest runs are safe. The ingestion trigger mechanism and SHA tracking are the open design questions for this LLD.
