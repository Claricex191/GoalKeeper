"""Policy versions: the single source of truth for field lists, thresholds and roles.

Lifecycle: draft → active → retired. Exactly one active policy per dataset.
Both scripts/seed_policies.py (manual) and policy/profiler.py (agent-drafted)
only ever create DRAFTS; a human activates them.
"""


def load_active(db, dataset: str) -> dict:
    """Return the active policy for this dataset (cached; refresh on activation)."""
    raise NotImplementedError


def create_draft(db, dataset: str, policy: dict, drafted_by: str) -> int:
    """Insert a new version with status "draft"; return its version number."""
    raise NotImplementedError


def activate(db, dataset: str, version: int, approved_by: str) -> None:
    """Human approval: retire the current active version, activate this one,
    and append {kind: "policy_change", dataset, from_version, to_version, user} to the
    ledger. Never called by the agent."""
    raise NotImplementedError


def detect_schema_drift(db, dataset: str) -> dict:
    """Compare the fields actually present in `employees` with the active policy's
    field_labels. System fields (_id, active, updated_at) are ignored.

    Returns {"new_fields": [...], "missing_fields": [...]}:
    - new_fields:     in the data, not in field_labels. Default deny: they cannot be
                      queried (L1 rejects) until labelled and approved by a human in a
                      new policy version (see profiler.draft_policy(fields=new_fields)).
    - missing_fields: in field_labels, no longer in the data; reported to the reviewer.
    Read-only: never changes the policy."""
    raise NotImplementedError


def tool_schema(policy: dict) -> dict:
    """Build the query_hr JSON schema (column enums, allowed metrics) from a policy,
    so the LLM only ever sees fields the active policy allows."""
    raise NotImplementedError
