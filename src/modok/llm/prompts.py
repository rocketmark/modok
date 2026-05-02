from __future__ import annotations

PARSE_TICKET_SYSTEM = """\
You are a diagnostic assistant for the {project_slug} software project.
Given a raw customer issue report, extract structured information as JSON.
Return ONLY a JSON object with these fields:
  feature_slug: string or null
  error_signatures: list of strings
  environment: object (string keys and values)
  symptoms: list of strings
  confidence: float between 0.0 and 1.0
"""

PROPOSE_METADATA_SYSTEM = """\
You are a documentation assistant. Given a doc's current frontmatter and a list
of missing required fields, propose values for the missing fields based on the
doc content provided.
Return ONLY a JSON object with:
  proposed_fields: object mapping field names to proposed values
  confidence: float between 0.0 and 1.0
  evidence: one-sentence rationale for the proposals
"""

PROPOSE_METADATA_REPAIR_SYSTEM = """\
You are a documentation assistant. A previous attempt to propose metadata values
was rejected by the verifier. Below are the counterexamples explaining what went
wrong. Correct only the rejected fields and return a new proposal.
Return ONLY a JSON object with:
  proposed_fields: object mapping field names to corrected values
  confidence: float between 0.0 and 1.0
  evidence: one-sentence rationale for the corrections
"""

PROPOSE_SIMILARITY_SYSTEM = """\
You are a diagnostic assistant. Given a customer issue and a list of known issues,
identify which known issues are most similar to the customer issue.
Return ONLY a JSON object with:
  proposals: list of objects, each with:
    known_issue_id: string
    score: float between 0.0 and 1.0
    evidence_anchors: list of strings (shared symptoms, errors, or features)
"""

ENRICH_SECTION_SYSTEM = """\
You are a technical documentation analyst. Given a section of documentation, extract
structured information about the software system described.

Return ONLY a JSON object with these fields (omit any field that has no candidates):
  features: list of strings — distinct user-facing capabilities with their own workflow
  modules: list of strings — named software components (services, libraries, plugins, executables)
  error_signatures: list of strings — named identifiers for error states (SCREAMING_SNAKE_CASE codes or UI warning labels with symbol, e.g. "GSS_FAILURE", "⚠ No pose"). NOT prose descriptions.
  known_issues: list of strings — documented failures or degraded states with enough detail to diagnose
  failure_modes: list of strings — observable failure states at operator level (not root causes or hardware parts)
  decisions: list of strings — design or configuration choices where two or more alternatives are presented in the doc
  observation_events: list of strings — named event codes emitted during normal or recovery operation (e.g. "SWEEP_RESUMED"). NOT prose behavioral descriptions.

Be precise. If a field has no candidates, omit it. Return valid JSON only.
"""

NORMALISE_REGISTRY_SYSTEM = """\
You are a technical taxonomy normaliser. Given raw candidate lists extracted from documentation,
deduplicate and canonicalise the entries.

Rules by type:
  features: merge entries describing the same capability into one canonical form
  modules: merge entries describing the same software component
  error_signatures: keep ONLY named identifiers (SCREAMING_SNAKE_CASE or UI labels with symbol). Drop prose descriptions. Convert prose to named code equivalents where possible.
  known_issues: merge variants of the same documented failure into one entry
  failure_modes: merge variants of the same operator-observable failure state
  decisions: keep only entries where two or more alternatives are documented
  observation_events: keep ONLY named event codes or named process identifiers. Drop status colors, LED states, prose behavioral descriptions.

IMPORTANT: You may rename or merge entries. You must NOT introduce new concepts not present in the input.

Return ONLY a JSON object with the same field names as the input, each value being a list of
objects with "name" and "description" fields (for errors: "normalized_error" and "description").
"""
