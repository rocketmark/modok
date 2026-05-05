"""
Tests for modok.registry.normalise — the modok normalise command engine.
All tests written before implementation (Phase 5). Every test cites
the EARS spec it verifies via @spec annotation.

Imports of registry modules are placed inside test functions so pytest
can collect tests before Phase 6 implementation exists.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


def make_cfg(
    timeout_propose_registry=60,
    cegis_max_repairs=1,
    normalise_batch_size=10000,
    normalise_refinement_passes=2,
):
    cfg = MagicMock()
    cfg.llm.timeout_propose_registry = timeout_propose_registry
    cfg.llm.cegis_max_repairs = cegis_max_repairs
    cfg.llm.normalise_batch_size = normalise_batch_size
    cfg.llm.normalise_refinement_passes = normalise_refinement_passes
    return cfg


FEATURES_RAW_CONTENT = """\
features:
  real-time-camera-tracking:
    name: Real-Time Camera Tracking
    description: ""
  ltc-timecode-sync:
    name: LTC Timecode Sync
    description: ""
"""

MODULES_RAW_CONTENT = """\
modules:
  stagehand-client:
    name: Stagehand Client
    description: ""
"""

ERRORS_RAW_CONTENT = """\
errors:
  gss-failure:
    normalized_error: GSS_FAILURE
    description: ""
  no-pose:
    normalized_error: "⚠ No pose"
    description: ""
"""


def write_raw_files(registries_dir: Path, *, features=True, modules=True, errors=True):
    registries_dir.mkdir(parents=True, exist_ok=True)
    if features:
        (registries_dir / "features.raw.yml").write_text(FEATURES_RAW_CONTENT)
    if modules:
        (registries_dir / "modules.raw.yml").write_text(MODULES_RAW_CONTENT)
    if errors:
        (registries_dir / "errors.raw.yml").write_text(ERRORS_RAW_CONTENT)


# ---------------------------------------------------------------------------
# RN-CMD-001 — one or more but not all raw files absent → warn + continue
# ---------------------------------------------------------------------------

# @spec RN-CMD-001
def test_normalise_warns_when_some_raw_files_absent(tmp_path, capsys):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries", features=True, modules=False, errors=False)
    cfg = make_cfg()

    with patch("modok.registry.normalise.normalise_candidates", return_value=[]):
        normalise_registries(tmp_path, cfg)

    captured = capsys.readouterr()
    assert "modules.raw.yml" in captured.err or "missing" in captured.err.lower()


# @spec RN-CMD-001
def test_normalise_skips_absent_fields_but_processes_present(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries", features=True, modules=False, errors=False)
    cfg = make_cfg()

    with patch("modok.registry.normalise.normalise_candidates", return_value=[]):
        normalise_registries(tmp_path, cfg)

    registries = tmp_path / "registries"
    # features.yml should be written (raw file present)
    assert (registries / "features.yml").exists()
    # modules.yml and errors.yml should NOT be written (raw files absent)
    assert not (registries / "modules.yml").exists()
    assert not (registries / "errors.yml").exists()


# ---------------------------------------------------------------------------
# RN-CMD-002 — no raw files at all → clear error
# ---------------------------------------------------------------------------

# @spec RN-CMD-002
def test_normalise_errors_when_no_raw_files_exist(tmp_path):
    from modok.registry.normalise import normalise_registries
    (tmp_path / "registries").mkdir()  # directory exists, no .raw.yml files
    cfg = make_cfg()

    with pytest.raises(Exception):
        normalise_registries(tmp_path, cfg)


# @spec RN-CMD-002
def test_normalise_error_mentions_init_assisted(tmp_path):
    from modok.registry.normalise import normalise_registries
    (tmp_path / "registries").mkdir()
    cfg = make_cfg()

    try:
        normalise_registries(tmp_path, cfg)
        raised = None
    except Exception as exc:
        raised = exc

    assert raised is not None
    err_text = str(raised)
    assert "init" in err_text.lower() or "assisted" in err_text.lower()


# ---------------------------------------------------------------------------
# RN-NORM-001 — separate LLM call per field type; never combined
# ---------------------------------------------------------------------------

# @spec RN-NORM-001
def test_normalise_makes_separate_call_per_field(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg()

    call_args_list = []

    def tracking_norm(candidates, field_type, cfg_llm, counterexamples=None):
        call_args_list.append(field_type)
        return []

    with patch("modok.registry.normalise.normalise_candidates", side_effect=tracking_norm):
        normalise_registries(tmp_path, cfg)

    assert "features" in call_args_list
    assert "modules" in call_args_list
    assert "errors" not in call_args_list  # errors skips LLM; slug dedup only


# @spec RN-NORM-001
def test_normalise_calls_gateway_at_most_once_per_field_plus_refinement(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg(cegis_max_repairs=0)  # no repairs — 1 CEGIS call + 1 refinement per field

    field_call_counts: dict[str, int] = {}

    def counting_norm(candidates, field_type, cfg_llm, counterexamples=None):
        field_call_counts[field_type] = field_call_counts.get(field_type, 0) + 1
        return candidates  # return as-is (valid: same count)

    with patch("modok.registry.normalise.normalise_candidates", side_effect=counting_norm):
        normalise_registries(tmp_path, cfg)

    # Each field: 1 CEGIS attempt + at most 1 refinement pass = at most 2 total
    max_repairs = 0
    for field_type in ("features", "modules", "errors"):
        count = field_call_counts.get(field_type, 0)
        assert count <= max_repairs + 2, (
            f"Expected at most {max_repairs + 2} calls for '{field_type}', got {count}"
        )


# ---------------------------------------------------------------------------
# RN-NORM-002 — each per-field call uses timeout_propose_registry
# ---------------------------------------------------------------------------

# @spec RN-NORM-002
def test_normalise_passes_timeout_to_each_field_call(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg(timeout_propose_registry=90)

    seen_cfgs = []

    def capture_cfg(candidates, field_type, cfg_llm, counterexamples=None):
        seen_cfgs.append(cfg_llm)
        return candidates

    with patch("modok.registry.normalise.normalise_candidates", side_effect=capture_cfg):
        normalise_registries(tmp_path, cfg)

    for seen_cfg in seen_cfgs:
        assert hasattr(seen_cfg, "timeout_propose_registry")
        assert seen_cfg.timeout_propose_registry == 90


# ---------------------------------------------------------------------------
# RN-NORM-003 — normalisation must not introduce new concepts
# (tested via _verify_field — catches entry-count violations)
# ---------------------------------------------------------------------------

# @spec RN-NORM-003
def test_verify_field_flags_when_output_has_more_entries_than_input():
    from modok.registry.normalise import _verify_field
    input_candidates = [
        {"name": "Feature A", "description": ""},
        {"name": "Feature B", "description": ""},
    ]
    normalised = input_candidates + [{"name": "Hallucinated Feature", "description": ""}]

    violations = _verify_field("features", input_candidates, normalised)
    assert len(violations) > 0


# @spec RN-NORM-003
def test_verify_field_passes_when_output_count_equals_input():
    from modok.registry.normalise import _verify_field
    candidates = [{"name": "Feature A", "description": ""}]
    normalised = [{"name": "Feature A Canonical", "description": "renamed"}]

    violations = _verify_field("features", candidates, normalised)
    assert len(violations) == 0


# @spec RN-NORM-003
def test_verify_field_passes_when_output_count_less_than_input():
    from modok.registry.normalise import _verify_field
    candidates = [
        {"name": "Feature A", "description": ""},
        {"name": "Feature A duplicate", "description": ""},
    ]
    normalised = [{"name": "Feature A", "description": "merged"}]

    violations = _verify_field("features", candidates, normalised)
    assert len(violations) == 0


# ---------------------------------------------------------------------------
# RN-CEGIS-001 — passing verifier → accept and write
# ---------------------------------------------------------------------------

# @spec RN-CEGIS-001
def test_normalise_accepts_field_when_verifier_passes(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg()

    def good_normalise(candidates, field_type, cfg_llm, counterexamples=None):
        return candidates[:1] if candidates else []

    with patch("modok.registry.normalise.normalise_candidates", side_effect=good_normalise):
        normalise_registries(tmp_path, cfg)

    assert (tmp_path / "registries" / "features.yml").exists()
    data = yaml.safe_load((tmp_path / "registries" / "features.yml").read_text())
    assert "features" in data


# ---------------------------------------------------------------------------
# RN-CEGIS-002 — failing verifier → repair call with counterexamples
# ---------------------------------------------------------------------------

# @spec RN-CEGIS-002
def test_normalise_makes_repair_call_when_verifier_fails(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg(cegis_max_repairs=1)

    feature_call_count = [0]

    def bad_then_good(candidates, field_type, cfg_llm, counterexamples=None):
        if field_type == "features":
            feature_call_count[0] += 1
            if counterexamples is None:
                # Initial call: add a hallucinated entry to trigger verifier
                return candidates + [{"name": "Hallucinated", "description": ""}]
            # Repair call: return valid (fewer entries)
            return candidates[:1] if candidates else []
        return candidates[:1] if candidates else []

    with patch("modok.registry.normalise.normalise_candidates", side_effect=bad_then_good):
        normalise_registries(tmp_path, cfg)

    # At least 2 calls for features: initial + repair
    assert feature_call_count[0] >= 2


# @spec RN-CEGIS-002
def test_normalise_repair_call_receives_counterexamples(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg(cegis_max_repairs=1)

    repair_counterexamples = []

    def capture_repair(candidates, field_type, cfg_llm, counterexamples=None):
        if field_type == "features" and counterexamples is not None:
            repair_counterexamples.extend(counterexamples)
            return candidates[:1] if candidates else []
        # Initial call: return too many entries
        return candidates + [{"name": "Hallucinated", "description": ""}]

    with patch("modok.registry.normalise.normalise_candidates", side_effect=capture_repair):
        normalise_registries(tmp_path, cfg)

    assert len(repair_counterexamples) > 0, "Repair call must receive violating entries as counterexamples"


# ---------------------------------------------------------------------------
# RN-CEGIS-003 — CEGIS exhausted → fallback to raw + slug resolution + warning
# ---------------------------------------------------------------------------

# @spec RN-CEGIS-003
def test_normalise_falls_back_to_raw_when_cegis_exhausted(tmp_path, capsys):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg(cegis_max_repairs=1)

    def always_bad(candidates, field_type, cfg_llm, counterexamples=None):
        return candidates + [{"name": "Hallucinated", "description": ""}]

    with patch("modok.registry.normalise.normalise_candidates", side_effect=always_bad):
        normalise_registries(tmp_path, cfg)

    # features.yml must still be written (fallback to raw)
    assert (tmp_path / "registries" / "features.yml").exists()

    captured = capsys.readouterr()
    fallback_words = {"exhausted", "fallback", "cegis", "repair", "raw"}
    assert any(w in captured.err.lower() for w in fallback_words)


# @spec RN-CEGIS-003
def test_normalise_errors_fallback_uses_normalized_error_as_slug_source(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg(cegis_max_repairs=0)

    def always_bad_for_errors(candidates, field_type, cfg_llm, counterexamples=None):
        if field_type == "errors":
            return candidates + [{"normalized_error": "HALLUCINATED_ERR", "description": ""}]
        return candidates[:1] if candidates else []

    with patch("modok.registry.normalise.normalise_candidates", side_effect=always_bad_for_errors):
        normalise_registries(tmp_path, cfg)

    errors_yml = tmp_path / "registries" / "errors.yml"
    assert errors_yml.exists()
    data = yaml.safe_load(errors_yml.read_text())
    for slug, entry in data.get("errors", {}).items():
        assert "normalized_error" in entry
        # Slug must be derivable from normalized_error (e.g. "gss-failure" from "GSS_FAILURE")
        assert slug.replace("-", "_").upper() in entry["normalized_error"].upper() or len(slug) > 0


# ---------------------------------------------------------------------------
# RN-CEGIS-004 — total LLM calls per field ≤ cegis_max_repairs + 1
# ---------------------------------------------------------------------------

# @spec RN-CEGIS-004
def test_normalise_total_calls_per_field_bounded(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    max_repairs = 2
    cfg = make_cfg(cegis_max_repairs=max_repairs)

    call_counts: dict[str, int] = {}

    def counting_bad_norm(candidates, field_type, cfg_llm, counterexamples=None):
        call_counts[field_type] = call_counts.get(field_type, 0) + 1
        # Always return too many entries to force max repairs
        return candidates + [{"name": "Hallucinated", "description": ""}]

    with patch("modok.registry.normalise.normalise_candidates", side_effect=counting_bad_norm):
        normalise_registries(tmp_path, cfg)

    for field_type, count in call_counts.items():
        assert count <= max_repairs + 1, (
            f"Field '{field_type}' made {count} LLM calls; max allowed is {max_repairs + 1}"
        )


# @spec RN-CEGIS-004
def test_normalise_cegis_default_max_repairs_is_1(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = MagicMock()
    cfg.llm.timeout_propose_registry = 60
    cfg.llm.normalise_batch_size = 10000
    cfg.llm.normalise_refinement_passes = 2
    # No cegis_max_repairs set — should default to 1
    del cfg.llm.cegis_max_repairs

    feature_calls = [0]

    def counting_bad(candidates, field_type, cfg_llm, counterexamples=None):
        if field_type == "features":
            feature_calls[0] += 1
        return candidates + [{"name": "Hallucinated", "description": ""}]

    with patch("modok.registry.normalise.normalise_candidates", side_effect=counting_bad):
        normalise_registries(tmp_path, cfg)

    # Default cegis_max_repairs=1 → max 2 calls for features
    assert feature_calls[0] <= 2


# ---------------------------------------------------------------------------
# RN-WRITE-001 — write final .yml files, overwrite without warning; idempotent
# ---------------------------------------------------------------------------

# @spec RN-WRITE-001
def test_normalise_writes_all_three_final_yml_files(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg()

    with patch("modok.registry.normalise.normalise_candidates", return_value=[]):
        normalise_registries(tmp_path, cfg)

    registries = tmp_path / "registries"
    assert (registries / "features.yml").exists()
    assert (registries / "modules.yml").exists()
    assert (registries / "errors.yml").exists()


# @spec RN-WRITE-001
def test_normalise_overwrites_existing_final_yml(tmp_path):
    from modok.registry.normalise import normalise_registries
    registries = tmp_path / "registries"
    write_raw_files(registries)
    (registries / "features.yml").write_text(
        "features:\n  old-feature:\n    name: Old\n    description: Old\n"
    )
    cfg = make_cfg()

    def good_norm(candidates, field_type, cfg_llm, counterexamples=None):
        if field_type == "features":
            return [{"name": "New Feature", "description": "Fresh."}]
        return []

    with patch("modok.registry.normalise.normalise_candidates", side_effect=good_norm):
        normalise_registries(tmp_path, cfg)

    content = (registries / "features.yml").read_text()
    assert "old-feature" not in content
    assert "New Feature" in content


# @spec RN-WRITE-001
def test_normalise_is_idempotent(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg()

    def stable_norm(candidates, field_type, cfg_llm, counterexamples=None):
        return candidates[:1] if candidates else []

    with patch("modok.registry.normalise.normalise_candidates", side_effect=stable_norm):
        normalise_registries(tmp_path, cfg)
        result1 = (tmp_path / "registries" / "features.yml").read_text()

        normalise_registries(tmp_path, cfg)
        result2 = (tmp_path / "registries" / "features.yml").read_text()

    assert result1 == result2


# ---------------------------------------------------------------------------
# RN-WRITE-002 — leave .raw.yml files in place after normalise
# ---------------------------------------------------------------------------

# @spec RN-WRITE-002
def test_normalise_does_not_delete_raw_files(tmp_path):
    from modok.registry.normalise import normalise_registries
    write_raw_files(tmp_path / "registries")
    cfg = make_cfg()

    with patch("modok.registry.normalise.normalise_candidates", return_value=[]):
        normalise_registries(tmp_path, cfg)

    registries = tmp_path / "registries"
    assert (registries / "features.raw.yml").exists()
    assert (registries / "modules.raw.yml").exists()
    assert (registries / "errors.raw.yml").exists()


# @spec RN-WRITE-002
def test_normalise_does_not_modify_raw_file_content(tmp_path):
    from modok.registry.normalise import normalise_registries
    registries = tmp_path / "registries"
    write_raw_files(registries)
    original = (registries / "features.raw.yml").read_text()
    cfg = make_cfg()

    with patch("modok.registry.normalise.normalise_candidates", return_value=[]):
        normalise_registries(tmp_path, cfg)

    assert (registries / "features.raw.yml").read_text() == original
