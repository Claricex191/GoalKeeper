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


def _require_current_schema(policy: dict) -> None:
    if policy.get("schema_version") != 2:
        raise ValueError("policy schema_version must be 2; legacy policies are retained "
                         "only for audit and must be replaced by a version 2 policy")


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
        _require_current_schema(doc)
        _active_cache[dataset] = doc
    _require_current_schema(_active_cache[dataset])
    return _active_cache[dataset]


def clear_cache() -> None:
    _active_cache.clear()


def create_draft(db, dataset: str, policy: dict, drafted_by: str) -> int:
    """Insert a new version with status "draft"; return its version number."""
    _require_current_schema(policy)
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
    A retired version 2 policy may be re-activated (rollback). Legacy policies can
    only be retired as part of the upgrade; never re-activated. All writes are in
    one transaction."""
    if not approved_by:
        raise ValueError("activation needs a human approver")
    target = db.policies.find_one({"dataset": dataset, "version": version})
    if target is None:
        raise LookupError(f"{dataset!r} has no version {version}")
    _require_current_schema(target)
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


def _nullable_enum(values: list[str]) -> dict:
    """One of `values`, or null. (The API rejects `type: [string, null]` with an enum.)"""
    if not values:
        return {"type": "null"}
    return {"anyOf": [{"type": "string", "enum": list(values)}, {"type": "null"}]}


def tool_schema(policy: dict, role: str, mode: str = "aggre_stats") -> dict:
    """Narrow each tool to v2 category grants without granting data access itself.

    Individual and aggregate tools have separate object schemas. Identity and data
    scope are server-owned and cannot appear in either schema. Gate remains the
    authority for field/metric combinations, scope, query validity and release rules.

    Strict-tool compatible: every object closes additional properties and requires
    all its properties; optional values are nullable. Runtime Pydantic validation
    enforces array lengths and other constraints omitted from the strict schema.
    """
    if mode not in ("aggre_stats", "individual"):
        raise ValueError(f"unknown query mode {mode!r}")
    if policy.get("schema_version") != 2:
        raise PermissionError("tool schemas require a version 2 policy")
    permissions = policy.get("roles", {}).get(role)
    if not permissions or permissions.get("scope") not in (
            "company", "assigned_department", "own_hr_department"):
        raise PermissionError("this role has no data scope")
    grants = permissions.get("grants", {})
    if not any(mode in modes for modes in grants.values()):
        raise PermissionError(f"this role has no {mode} grant")
    labels = policy["field_labels"]
    never = set(policy.get("never_return", []))
    allowed = {
        field for field, category in policy["field_categories"].items()
        if mode in grants.get(category, []) and field in labels
        and labels[field] != "identifier" and field not in never
    }
    if mode == "individual":
        if not allowed:
            raise PermissionError("this role has no returnable individual fields")
        properties = {
            "mode": {"type": "string", "enum": ["individual"]},
            "employee_numbers": {
                "type": "array", "items": {"type": "integer"},
                "description": "1 to 100 distinct employee numbers to look up. "
                               "The server restricts them to your authorized scope.",
            },
            "fields": {"type": "array", "items": {"type": "string", "enum": sorted(allowed)},
                       "description": "The authorized fields to return for each requested employee."},
        }
        return {"type": "object", "additionalProperties": False,
                "required": list(properties), "properties": properties}

    field_metrics = policy["field_metrics"]
    numeric_metrics = {"sum", "avg", "median"}
    targets = sorted(field for field in allowed
                     if numeric_metrics.intersection(field_metrics.get(field, [])))
    offered = {metric for field in targets for metric in field_metrics[field]
               if metric in numeric_metrics}
    if "aggre_stats" in grants.get("other_basic", []):
        offered.add("count")
    metrics = [metric for metric in ("count", "sum", "avg", "median") if metric in offered]
    if not metrics:
        raise PermissionError("this role has no applicable statistical operations")
    if targets:
        # Keep explicit refusals and the T4 rewrite from the main tool contract.
        metrics += ["max", "min"]
    quasi = [field for field in policy["quasi_identifiers"] if field in allowed]
    scalar = {"type": ["string", "number"]}
    filters = {
        "type": "array",
        "description": "Conditions on who is included; all must hold (AND). "
                       "The server's department scope cannot be removed.",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["col", "op", "val"],
            "properties": {
                "col": {"type": "string", "enum": quasi},
                "op": {"type": "string", "enum": ["=", "!=", "<", "<=", ">", ">=", "in"]},
                "val": {"anyOf": [scalar, {"type": "array", "items": scalar}],
                        "description": "A list only with op 'in'."},
            },
        },
    }
    grouping = {"type": "array", "items": {"type": "string", "enum": quasi},
                "description": "At most 2 columns; [] for a single overall number."}
    if not quasi:
        # Empty enums are invalid JSON Schema. Null items keep the strict schema
        # valid without advertising unauthorized columns. Only [] is accepted by
        # the actual StructuredQuery; nonempty null arrays fail runtime validation.
        filters = {"type": "array", "items": {"type": "null"},
                   "description": "No filter fields are authorized; use []."}
        grouping = {"type": "array", "items": {"type": "null"},
                    "description": "No grouping fields are authorized; use []."}
    target = (_nullable_enum(targets) if "count" in offered else
              {"type": "string", "enum": targets})
    properties = {
        "mode": {"type": "string", "enum": ["aggre_stats"]},
        "filters": filters,
        "group_by": grouping,
        "metric": {"type": "string", "enum": metrics},
        "target": {**target, "description": "The field to aggregate; null only for count."},
        "transform": {
            **_nullable_enum(["T1_merge_groups", "T2_drop_filter", "T3_widen_scope",
                              "T4_extreme_to_avg"]),
            "description": "null on a first attempt. After a block, the transform you "
                           "applied (must be one the block allowed).",
        },
    }
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}
