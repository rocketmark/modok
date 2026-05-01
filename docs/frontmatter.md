# MODOK Frontmatter Reference

Every doc that MODOK should ingest must carry a `modok:` block in its YAML frontmatter. Without it the file is silently skipped.

## Minimal example

```yaml
---
modok:
  doc_type: lld
  feature: shtp-receiver
  modules:
    - shtp
  source_files:
    - agent/src/shtp.c
    - agent/src/shtp.h
  test_files:
    - agent/tests/test_shtp_receiver.py
---
```

## Fields

### `doc_type` (required)

Declares what kind of document this is. Determines which other fields are required.

| Value | Description | Required fields |
|---|---|---|
| `hld` | High-level design | `feature` |
| `lld` | Low-level design | `feature`, `modules`, `source_files`, `test_files` |
| `spec` | EARS spec file | `feature` |
| `testing` | Test plan or test notes | `feature`, `test_files` |
| `adr` | Architecture decision record | `feature` |
| `runbook` | Operational runbook | `feature` |
| `known-issue` | Known issue writeup | `feature`, `error_signatures` |
| `release-notes` | Release notes | `feature` |

### `feature` (required for most doc types)

The feature slug this document describes. Must match an entry in `registries/features.yml`.

```yaml
feature: shtp-receiver
```

### `modules` (required for `lld`)

The module slugs implemented by the feature this doc covers. Each must match an entry in `registries/modules.yml`. MODOK uses this to write `IMPLEMENTED_BY` edges from the Feature node to each Module node.

```yaml
modules:
  - shtp
  - recovery
```

### `source_files` (required for `lld`)

Paths to source files, relative to the repo root. MODOK writes `DEFINED_IN` edges from each Module to the corresponding File nodes. Files that don't exist on disk produce a warning but don't block ingestion.

```yaml
source_files:
  - agent/src/shtp.c
  - agent/src/shtp.h
```

### `test_files` (required for `lld`, `testing`)

Paths to test files, relative to the repo root. Treated the same as `source_files` — written as File nodes with `DEFINED_IN` edges.

```yaml
test_files:
  - agent/tests/test_shtp_receiver.py
```

### `product_area` (optional)

The product area slug this feature belongs to. Written as a `product_area_slug` property on the Feature node.

```yaml
product_area: networking
```

### `error_signatures` (required for `known-issue`, optional otherwise)

Error signature slugs associated with this doc. Each must match an entry in `registries/errors.yml`. MODOK writes `HAS_ERROR` edges from the Feature to each ErrorSignature node.

```yaml
error_signatures:
  - shtp-version-mismatch
  - shtp-seq-overflow
```

---

## Complete example (LLD)

```yaml
---
modok:
  doc_type: lld
  feature: pi-agent
  product_area: firmware
  modules:
    - shtp
    - recovery
    - health-monitor
  source_files:
    - agent/src/main.c
    - agent/src/shtp.c
    - agent/src/shtp.h
    - agent/src/recovery.c
    - agent/src/recovery.h
  test_files:
    - agent/tests/test_main_loop.py
    - agent/tests/test_scenarios.py
    - agent/tests/test_e2e.py
  error_signatures:
    - imu-watchdog-timeout
---
```

---

## Registries

Slugs referenced in frontmatter must be declared in the registries under `registries/` in your repo root. MODOK validates all slugs at ingest time and halts ingestion for any file with an unknown slug.

**`registries/features.yml`**
```yaml
features:
  pi-agent:
    name: Pi Agent
    description: Pose acquisition, SHTP encoding, recovery, and health monitoring.
    modules:
      - shtp
      - recovery
      - health-monitor
```

**`registries/modules.yml`**
```yaml
modules:
  shtp:
    name: SHTP Encoder/Transmitter
    source_roots:
      - agent/src
  recovery:
    name: Recovery State Machine
    source_roots:
      - agent/src
```

**`registries/errors.yml`**
```yaml
errors:
  imu-watchdog-timeout:
    text: "IMU watchdog: no data for 5s"
    feature: pi-agent
```

Run `modok ingest --project <slug> <path>` to check for slug errors before committing. Missing or misspelled slugs are reported as errors in the ingestion report.

---

## What MODOK does with this

When a doc is ingested, MODOK creates or updates:

- A **Feature** node for `feature:`
- A **Module** node for each entry in `modules:`
- A **File** node for each entry in `source_files:` and `test_files:`
- A **DocSection** node for each H2/H3 heading in the doc body
- An **ErrorSignature** node for each entry in `error_signatures:`

And writes edges:
- `Feature -[IMPLEMENTED_BY]-> Module` for each module
- `Module -[DEFINED_IN]-> File` for each source/test file
- `Feature -[DESCRIBED_BY]-> DocSection` for each heading
- `Feature -[HAS_ERROR]-> ErrorSignature` for each error signature

All writes are idempotent — ingesting the same doc twice produces the same graph state.
