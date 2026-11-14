"""Policy versions: the single source of truth for field lists, thresholds and roles.

Lifecycle: draft → active → retired. Exactly one active policy per dataset.
Both scripts/seed_policies.py (manual) and policy/profiler.py (agent-drafted)
only ever create DRAFTS; a human activates them.
"""

from pymongo import ASCENDING, DESCENDING

from app import clock
from app.memory import ledger

# Keys that only the lifecycle functions below may set.
_LIFECYCLE_KEYS = {"_id", "dataset", "version", "status", "drafted_by", "approved_by",
                   "created_at", "activated_at", "retired_at"}

_active_cache: dict[str, dict] = {}


def ensure_policy_indexes(db) -> None:
    """At most one active version per dataset, enforced by the database itself."""
    db.policies.create_index([("dataset", ASCENDING), ("version", ASCENDING)], unique=True)
    db.policies.create_index([("dataset", ASCENDING)], unique=True,
                             name="one_active_per_dataset",
                             partialFilterExpression={"status": "active"})


def load_active(db, dataset: str) -> dict:
    """Return the active policy for this dataset (cached; refresh on activation)."""
    if dataset not in _active_cache:
        doc = db.policies.find_one({"dataset": dataset, "status": "active"})
        if doc is None:
            raise LookupError(f"no active policy for {dataset!r}; run scripts/seed_policies.py")
        _active_cache[dataset] = doc
    return _active_cache[dataset]


def clear_cache() -> None:
    _active_cache.clear()


def create_draft(db, dataset: str, policy: dict, drafted_by: str) -> int:
    """Insert a new version with status "draft"; return its version number."""
    last = db.policies.find_one({"dataset": dataset}, sort=[("version", DESCENDING)])
    version = last["version"] + 1 if last else 1
    body = {k: v for k, v in policy.items() if k not in _LIFECYCLE_KEYS}
    db.policies.insert_one({
        **body,
        "_id": f"{dataset}_v{version}",
        "dataset": dataset,
        "version": version,
        "status": "draft",
        "drafted_by": drafted_by,
        "created_at": clock.now(),
    })
    return version


def activate(db, dataset: str, version: int, approved_by: str) -> None:
    """Human approval: retire the current active version, activate this one,
    and append {kind: "policy_change", dataset, from_version, to_version, user} to the
    ledger. Never called by the agent.
    A retired version may be re-activated (rollback). All writes in one transaction."""
    if not approved_by:
        raise ValueError("activation needs a human approver")
    target = db.policies.find_one({"dataset": dataset, "version": version})
    if target is None:
        raise LookupError(f"{dataset!r} has no version {version}")
    if target["status"] == "active":
        return
    current = db.policies.find_one({"dataset": dataset, "status": "active"})
    at = clock.now()

    def txn(session):
        if current:
            db.policies.update_one({"_id": current["_id"]},
                                   {"$set": {"status": "retired", "retired_at": at}},
                                   session=session)
        db.policies.update_one({"_id": target["_id"]},
                               {"$set": {"status": "active", "approved_by": approved_by,
                                         "activated_at": at}},
                               session=session)
        ledger.append(db, {"kind": "policy_change", "time": at, "user": approved_by,
                           "dataset": dataset,
                           "from_version": current["version"] if current else None,
                           "to_version": version}, session=session)

    with db.client.start_session() as session:
        session.with_transaction(txn)
    _active_cache.pop(dataset, None)


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
