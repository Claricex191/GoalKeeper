"""STRETCH GOAL: agent drafts a policy for a new dataset; a human approves it.

Steps
1. Deterministic profiling per column: dtype, distinct count, share of unique values,
   sample values.
   - near-unique (e.g. > 95% distinct)        → "identifier"  (never returnable)
   - low-cardinality demographic-like values   → "quasi_identifier" candidate
2. LLM labels the rest from column names + samples: "sensitive" / "quasi_identifier" /
   "other", with a one-line reason each.
3. When unsure → label "sensitive" (fail closed).
4. Defaults: k_threshold = 5 (10 if any column looks health-related),
   median_threshold = 10, memory scope = team.
5. store.create_draft(...); the reviewer sees the labels and reasons, edits, activates.

Incremental mode (schema drift): when store.detect_schema_drift() reports new_fields,
profile and label ONLY those fields, copy everything else from the active policy, and
save the result as a new draft version. Until a human activates it, the new fields stay
unqueryable (L1 default deny).
"""


def profile_columns(db, dataset: str, sample_size: int = 200,
                    fields: list[str] | None = None) -> list[dict]:
    """Profile all columns, or only `fields` when given."""
    raise NotImplementedError


def draft_policy(db, dataset: str, fields: list[str] | None = None) -> int:
    """Run profiling + LLM labelling and save a draft; return the draft version.
    fields=None → full draft for a new dataset.
    fields=[...] → incremental draft: label only these (e.g. new_fields from
    store.detect_schema_drift), carry over the rest of the active policy."""
    raise NotImplementedError
