# Using LLM Fix — Current State and Next Steps

## What exists today

`run_llm_proposal_pass` is fully implemented and tested. Given a doc path,
its current frontmatter, and a list of missing fields, it:

1. Calls the local Ollama model to propose values
2. Verifies the proposal (scope, type, slug, enum, duplicates, empty, evidence)
3. If `cegis_fix_enabled = true` and fields were rejected, makes one repair
   attempt with the rejection reasons as context
4. In default mode: writes valid fields, warns on rejected
5. In strict mode: writes nothing if any field is still rejected after repair
6. In dry-run mode: makes LLM calls but writes nothing to disk

What does **not** exist yet: a CLI entry point. The `modok ingest --fix` flag
routes through the old `invoke_llm_gateway` stub, not the new function.

---

## How to test it today

Requires Ollama running with gemma4 pulled, and Quine running.

### 1. Find a doc with missing frontmatter

Pick any markdown doc in your project repo that has a `modok:` frontmatter
block but is missing `feature`, `modules`, `source_files`, or `test_files`.
Or create a minimal test doc:

```bash
cat > /tmp/test-doc.md << 'EOF'
---
modok:
  doc_type: lld
  project: stagehand
---
# SHTP Receiver

The SHTP receiver module parses incoming UDP packets from the Pi agent.
It validates the boot_id epoch, applies the seq_newer drop-on-late rule,
and forwards valid poses to the livelink bus.

Source: `client/stagehand_client/shtp_receiver.py`
Tests: `client/tests/test_shtp_receiver.py`
EOF
```

### 2. Run the proposal script

Create a small script (don't need to install anything extra — just activate
the modok venv):

```bash
cat > /tmp/test_fix.py << 'EOF'
import asyncio
from pathlib import Path
from modok.ingestion.registry import Registry
from modok.ingestion.pipeline import run_llm_proposal_pass

DOC = Path("/tmp/test-doc.md")
REPO = Path.home() / "github" / "stagehand"

async def main():
    registry = Registry(REPO)
    result = await run_llm_proposal_pass(
        doc_path=DOC,
        frontmatter={"doc_type": "lld", "project": "stagehand"},
        missing_fields=["feature", "modules", "source_files", "test_files"],
        registry=registry,
        strict=False,
        dry_run=True,   # set False to actually write to the doc
    )
    print(f"suppressed:   {result.suppressed}")
    print(f"dry_run:      {result.dry_run}")
    print(f"wrote_nothing:{result.wrote_nothing}")
    print(f"valid_fields: {result.valid_fields}")
    print(f"warnings:     {[(w.field, w.reason) for w in result.warnings]}")
    print(f"errors:       {result.errors}")

asyncio.run(main())
EOF
python3 /tmp/test_fix.py
```

### 3. What to look for

- **`valid_fields`** — fields the verifier accepted; these would be written to
  the doc if `dry_run=False`
- **`warnings`** — fields the LLM proposed but the verifier rejected, with
  reasons (wrong slug, wrong type, filler evidence, etc.)
- **`errors`** — only populated in strict mode when fields are still rejected
  after the repair attempt
- **`suppressed = True`** — means `MODOK_NON_INTERACTIVE=1` was set or the
  function returned early for another reason

### 4. Test the CEGIS repair loop

To see the repair attempt fire, use a model config where the first call is
likely to produce a slug that fails registry validation. Set
`cegis_fix_enabled = true` in `~/.modok/config.toml` (it should already be
there from setup). The second `propose_metadata` call will include the
rejection reasons as context.

To confirm two LLM calls were made, temporarily add a print to the script:

```python
from unittest.mock import patch
from modok.llm import gateway as gw

original = gw.propose_metadata
call_count = 0

async def counting_propose(*args, **kwargs):
    global call_count
    call_count += 1
    print(f"  LLM call #{call_count} repair_context={kwargs.get('repair_context') is not None}")
    return await original(*args, **kwargs)

with patch.object(gw, "propose_metadata", new=counting_propose):
    result = await run_llm_proposal_pass(...)

print(f"Total LLM calls: {call_count}")
```

### 5. Test strict mode

Change `dry_run=True` to `dry_run=False, strict=True`. If any field is still
rejected after repair, `result.wrote_nothing` will be `True` and `result.errors`
will list the reasons. The doc will not be modified.

---

## What needs to be wired up next

### Wire `run_llm_proposal_pass` into `ingest_doc`

The call site is in `check_required_fields` inside
`src/modok/ingestion/pipeline.py`. Currently when `fix=True` and fields are
missing it calls `invoke_llm_gateway`, the old stub. That needs to be replaced
with a call to `run_llm_proposal_pass`.

The challenge: `ingest_doc` is not async-aware at the `check_required_fields`
level — it uses `asyncio.run()` inside a sync function. The cleanest path is
to lift the LLM proposal pass out of `check_required_fields` and into
`ingest_doc` directly, after the missing-fields check, as an `await`.

Rough shape of the change in `ingest_doc`:

```python
if ctx.fix_mode and missing:
    llm_result = await run_llm_proposal_pass(
        doc_path=path,
        frontmatter=fm,
        missing_fields=missing,
        registry=registry,
        strict=ctx.strict,
        dry_run=ctx.dry_run,
        emit_counterexamples=ctx.emit_counterexamples,
    )
    # llm_result.valid_fields already written to disk by run_llm_proposal_pass
    # re-parse fm from disk to pick up the patch
    fm = parse_frontmatter(path) or fm
    for w in llm_result.warnings:
        ctx.add_warning(f"LLM rejected field '{w.field}': {w.reason}")
    for e in llm_result.errors:
        ctx.add_warning(f"LLM strict rejection: {e}")
```

`IngestionContext` needs `strict`, `dry_run`, and `emit_counterexamples` fields
added alongside `fix_mode`.

### Wire the CLI flags into `modok ingest`

`src/modok/cli/commands/ingest.py` (or wherever the ingest command lives) needs:

```
--fix                    enable LLM metadata proposals for missing fields
--strict                 reject doc entirely if any field fails after repair
--dry-run                make LLM calls but write nothing
--emit-counterexamples   write rejected-field YAML fixtures to counterexample_fixture_dir
```

These map directly to specs `CLI-INGEST-006` through `CLI-INGEST-009` in
`docs/specs/cli.md`.

### LID arrow check before implementing

Before writing the code, verify:
- `docs/llds/ingestion-pipeline.md` Stage 7 describes the caller
  responsibilities for `run_llm_proposal_pass` — confirm it matches the
  wire-up shape above
- `docs/specs/ingestion-pipeline.md` `SI-LLM-003` through `SI-LLM-010` are
  the specs the implementation must satisfy
- `tests/test_ingestion_pipeline.py` already has tests for all ten specs —
  they test `run_llm_proposal_pass` directly, not through the CLI, so new
  integration tests through `run_ingestion` may be needed
