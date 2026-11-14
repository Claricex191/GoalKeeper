"""Turn a StructuredQuery into one Atlas aggregation and return cells.

One generic function covers every supported question. No raw-record access, no joins.
Results stay on the server until the gate has decided.

Role-agnostic on purpose: who may ask what is decided BEFORE this runs (L1 pre-check in
agent.tools) and again inside the gate. This module only computes.

Errors are raised as QueryError with a short message the agent can show the LLM so it
can fix the query (unknown column, wrong value type, non-numeric target...).
"""
from datetime import datetime

from bson.int64 import Int64

from app.schemas import Cell, StructuredQuery

OPS = {"=": "$eq", "!=": "$ne", "<": "$lt", "<=": "$lte",
       ">": "$gt", ">=": "$gte", "in": "$in"}

# Fields the app adds; never used as filters, groups or targets.
SYSTEM_FIELDS = {"_id", "active", "start_date", "updated_at"}

_field_types_cache: dict[str, type] | None = None


class QueryError(ValueError):
    """The query cannot run as written; the message is safe to show to the LLM."""


# --- helpers ------------------------------------------------------------------------
def _check_name(col: str, field_types: dict[str, type]) -> None:
    if col.startswith("$") or "." in col or col in SYSTEM_FIELDS:
        raise QueryError(f"invalid column name {col!r}")
    if col not in field_types:
        raise QueryError(f"unknown column {col!r}")


def _coerce(col: str, val, field_types: dict[str, type]):
    """Match the stored type exactly: MongoDB returns nothing (silently) for "3" vs 3."""
    want = field_types[col]
    try:
        if want is int:
            if isinstance(val, bool):
                raise ValueError
            f = float(val)
            if not f.is_integer():
                raise ValueError
            return int(f)
        if want is float:
            return float(val)
        if want is str:
            return str(val)
    except (TypeError, ValueError):
        raise QueryError(f"{col} expects {want.__name__} values, got {val!r}") from None
    return val


def clear_cache() -> None:
    """Forget the sampled schema after restoring the demo dataset."""
    global _field_types_cache
    _field_types_cache = None


def field_types(db, *, session=None) -> dict[str, type]:
    """Column → Python type, sampled once outside a transaction.

    Transactional callers read their own snapshot and never populate the process
    cache with uncommitted data (a reset may still roll back).
    """
    global _field_types_cache
    if _field_types_cache is None or session is not None:
        options = {"session": session} if session is not None else {}
        sample = db.employees.find_one({}, {f: 0 for f in SYSTEM_FIELDS}, **options)
        if sample is None:
            raise QueryError("employees is empty: run scripts/load_data.py")
        # BSON int64 decodes to an Int64 subclass, but is still an integer
        # column. Preserve bool as bool rather than treating every int subclass
        # alike; bool targets must not become numeric averages.
        types = {k: int if isinstance(v, Int64) else type(v) for k, v in sample.items()}
        if session is not None:
            return types
        _field_types_cache = types
    return _field_types_cache


# --- public -------------------------------------------------------------------------
def build_pipeline(q: StructuredQuery, now: datetime,
                   types: dict[str, type]) -> list[dict]:
    """$match (each filter its own clause under $and, plus active == True and
    start_date <= now) → $group by q.group_by with n, ids ($push EmployeeNumber),
    value ($avg/$sum on target) → $sort by group key.
    `now` is clock.now() (simulated), so advancing the demo clock past a new hire's
    start_date adds them to every population. `types` comes from field_types(db);
    passing it in keeps this function pure and testable without a database."""
    clauses: list[dict] = [{"active": True}, {"start_date": {"$lte": now}}]
    for f in q.filters:
        _check_name(f.col, types)
        if f.op == "in":
            if not isinstance(f.val, list) or not f.val:
                raise QueryError(f"'in' on {f.col} needs a non-empty list")
            val = [_coerce(f.col, v, types) for v in f.val]
        else:
            if isinstance(f.val, list):
                raise QueryError(f"'{f.op}' on {f.col} needs a single value, not a list")
            val = _coerce(f.col, f.val, types)
        clauses.append({f.col: {OPS[f.op]: val}})

    for col in q.group_by:
        _check_name(col, types)
    group_id = {col: f"${col}" for col in q.group_by} if q.group_by else None

    group: dict = {"_id": group_id, "n": {"$sum": 1}, "ids": {"$push": "$EmployeeNumber"}}
    if q.metric not in ("count", "sum", "avg"):
        raise QueryError(f"metric '{q.metric}' is not executed")
    if q.metric in ("sum", "avg"):
        if q.target is None:
            raise QueryError(f"metric '{q.metric}' needs a target column")
        _check_name(q.target, types)
        if types[q.target] not in (int, float):
            raise QueryError(f"{q.target} is not numeric; use metric 'count' with a filter "
                             f"on it instead (e.g. {q.target} = <value>)")
        group["value"] = {f"${q.metric}": f"${q.target}"}
        group["vmin"] = {"$min": f"${q.target}"}
        group["vmax"] = {"$max": f"${q.target}"}

    return [
        {"$match": {"$and": clauses}},
        {"$group": group},
        {"$sort": {"_id": 1}},
    ]


def run_query(db, q: StructuredQuery, now: datetime, *, session=None) -> list[Cell]:
    """Run the aggregation; one Cell per non-empty group (empty groups never appear).
    Values are not rounded here; rounding is a display concern."""
    cells = []
    options = {"session": session} if session is not None else {}
    pipeline = build_pipeline(q, now, field_types(db, session=session))
    for row in db.employees.aggregate(pipeline, **options):
        ids = {int(i) for i in row["ids"]}
        value = row.get("value")
        cells.append(Cell(
            key=dict(row["_id"]) if row["_id"] else {},
            ids=ids,
            n=len(ids),
            value=float(value) if value is not None else None,
            vmin=float(row["vmin"]) if row.get("vmin") is not None else None,
            vmax=float(row["vmax"]) if row.get("vmax") is not None else None,
        ))
    return cells
