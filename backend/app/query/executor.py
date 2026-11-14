"""Execute authorized HR queries within a server-owned data scope.

Every entry point rechecks authorization before accessing employee data. Statistical
results and individual records remain server-only until the corresponding Gate check.
"""
from datetime import datetime
from math import isfinite
from statistics import median

from app.policy.access import authorized_departments
from app.schemas import Cell, Identity, IndividualQuery, IndividualRecord, StructuredQuery

OPS = {"=": "$eq", "!=": "$ne", "<": "$lt", "<=": "$lte",
       ">": "$gt", ">=": "$gte", "in": "$in"}
SYSTEM_FIELDS = {"_id", "active", "start_date", "updated_at"}
_field_types_cache: dict[str, type] | None = None


class QueryError(ValueError):
    """A safe, value-free explanation that can be shown to the query caller."""


def _authorize(q: StructuredQuery | IndividualQuery, who: Identity, policy: dict):
    from app.gate import precheck

    if not isinstance(who, Identity):
        raise QueryError("A server-resolved identity is required")
    decision = precheck(q, who, policy)
    if decision is not None:
        raise QueryError(f"Query is not authorized ({decision.rule})")
    try:
        return authorized_departments(who, policy)
    except (PermissionError, ValueError, KeyError, TypeError):
        raise QueryError("No authorized data scope") from None


def _base_clauses(now: datetime, departments: tuple[str, ...] | None) -> list[dict]:
    clauses = [{"active": True}, {"start_date": {"$lte": now}}]
    if departments is not None:
        # Separate AND clause: no user filter or rewrite can overwrite this condition.
        clauses.append({"Department": {"$in": list(departments)}})
    return clauses


def _check_name(col: str, types: dict[str, type]) -> None:
    if col.startswith("$") or "." in col or col in SYSTEM_FIELDS:
        raise QueryError(f"invalid column name {col!r}")
    if col not in types:
        raise QueryError(f"unknown column {col!r}")


def _coerce(col: str, val, types: dict[str, type]):
    """Match stored types: a numeric string should not silently match zero people."""
    want = types[col]
    try:
        if want is int:
            if isinstance(val, bool):
                raise ValueError
            number = float(val)
            if not isfinite(number) or not number.is_integer():
                raise ValueError
            return int(number)
        if want is float:
            number = float(val)
            if not isfinite(number):
                raise ValueError
            return number
        if want is str:
            return str(val)
    except (TypeError, ValueError, OverflowError):
        raise QueryError(f"{col} expects {want.__name__} values") from None
    return val


def field_types(db, now: datetime, scope: tuple[str, ...] | None) -> dict[str, type]:
    """Read schema types only from the authorized, currently active population."""
    global _field_types_cache
    if _field_types_cache is None:
        sample = db.employees.find_one(
            {"$and": _base_clauses(now, scope)}, {f: 0 for f in SYSTEM_FIELDS})
        if sample is None:
            raise QueryError("No active employees in the authorized scope")
        _field_types_cache = {key: type(value) for key, value in sample.items()}
    return _field_types_cache


def build_pipeline(q: StructuredQuery, now: datetime, types: dict[str, type],
                   who: Identity, policy: dict) -> list[dict]:
    """Build a scoped aggregation; inputs contain no client-controlled scope."""
    if not isinstance(q, StructuredQuery):
        raise QueryError("A statistical query is required")
    departments = _authorize(q, who, policy)
    clauses = _base_clauses(now, departments)
    for filt in q.filters:
        _check_name(filt.col, types)
        if filt.op == "in":
            if not isinstance(filt.val, list) or not filt.val:
                raise QueryError(f"'in' on {filt.col} needs a non-empty list")
            value = [_coerce(filt.col, item, types) for item in filt.val]
        else:
            if isinstance(filt.val, list):
                raise QueryError(f"'{filt.op}' on {filt.col} needs a single value")
            value = _coerce(filt.col, filt.val, types)
        clauses.append({filt.col: {OPS[filt.op]: value}})
    for col in q.group_by:
        _check_name(col, types)

    group = {
        "_id": {col: f"${col}" for col in q.group_by} if q.group_by else None,
        "n": {"$sum": 1},
        "ids": {"$push": "$EmployeeNumber"},
        "departments": {"$addToSet": "$Department"},
    }
    if q.metric not in ("count", "sum", "avg", "median"):
        raise QueryError(f"metric '{q.metric}' is not executed")
    if q.metric != "count":
        if q.target is None:
            raise QueryError(f"metric '{q.metric}' needs a target column")
        _check_name(q.target, types)
        if types[q.target] not in (int, float):
            raise QueryError(f"{q.target} is not numeric")
        # Retain values only inside the executor. Checking all contributors prevents
        # Mongo's avg/sum behavior of silently skipping null or nonnumeric values from
        # making a 4-person mean appear to be a 5-person mean. It also supports an
        # exact median without depending on MongoDB's approximate $median operator.
        group["values"] = {"$push": f"${q.target}"}
    return [{"$match": {"$and": clauses}}, {"$group": group}, {"$sort": {"_id": 1}}]


def _number(value) -> bool:
    try:
        return type(value) in (int, float) and isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _departments(values, scope: tuple[str, ...] | None) -> set[str]:
    if not isinstance(values, list) or not values or any(
            not isinstance(value, str) or not value for value in values):
        raise QueryError("Employee data has missing department information")
    departments = set(values)
    if scope is not None and not departments.issubset(scope):
        raise QueryError("Employee data is outside the authorized scope")
    return departments


def run_query(db, q: StructuredQuery, now: datetime,
              who: Identity, policy: dict) -> list[Cell]:
    """Return statistical cells with scope evidence, never individual input values."""
    if not isinstance(q, StructuredQuery):
        raise QueryError("A statistical query is required")
    scope = _authorize(q, who, policy)  # Must precede even the schema sample read.
    pipeline = build_pipeline(q, now, field_types(db, now, scope), who, policy)
    cells = []
    for row in db.employees.aggregate(pipeline):
        raw_ids = row.get("ids", [])
        if not raw_ids or any(type(item) is not int for item in raw_ids):
            raise QueryError("Employee data has invalid identifiers")
        ids = set(raw_ids)
        if row.get("n") != len(ids) or len(raw_ids) != len(ids):
            raise QueryError("Employee data has inconsistent population counts")
        departments = _departments(row.get("departments"), scope)
        key = row.get("_id") or {}
        if not isinstance(key, dict) or set(key) != set(q.group_by):
            raise QueryError("Employee data has inconsistent group keys")
        value = vmin = vmax = None
        if q.target is not None:
            values = row.get("values", [])
            if len(values) != len(ids) or not all(_number(item) for item in values):
                raise QueryError("Target data must be present, numeric and finite for every employee")
            try:
                if q.metric == "median":
                    value = median(values)
                else:
                    value = sum(values)
                    if q.metric == "avg":
                        value /= len(values)
            except (ValueError, OverflowError, TypeError):
                raise QueryError("Statistic is not finite") from None
            if not _number(value):
                raise QueryError("Statistic is not finite")
            vmin, vmax = min(values), max(values)
        cells.append(Cell(key=key, ids=ids, n=len(ids),
                          value=float(value) if value is not None else None,
                          vmin=float(vmin) if vmin is not None else None,
                          vmax=float(vmax) if vmax is not None else None,
                          departments=departments))
    return cells


def run_individual_query(db, q: IndividualQuery, now: datetime,
                         who: Identity, policy: dict) -> list[IndividualRecord]:
    """Read named employees using authorized fields and a mandatory department scope.

    Employee numbers and departments in the result are server-only evidence for Gate.
    They are not added to the requested field projection shown to the agent.
    """
    if not isinstance(q, IndividualQuery):
        raise QueryError("An individual query is required")
    scope = _authorize(q, who, policy)
    clauses = _base_clauses(now, scope)
    clauses.append({"EmployeeNumber": {"$in": list(q.employee_numbers)}})
    projection = {field: 1 for field in q.fields}
    projection.update({"_id": 0, "EmployeeNumber": 1, "Department": 1})
    records = []
    seen = set()
    for row in db.employees.find({"$and": clauses}, projection):
        employee = row.get("EmployeeNumber")
        if type(employee) is not int or employee not in q.employee_numbers or employee in seen:
            raise QueryError("Employee data has invalid identifiers")
        seen.add(employee)
        department = row.get("Department")
        _departments([department], scope)
        if any(field not in row for field in q.fields):
            raise QueryError("Requested employee fields are missing")
        values = {field: row[field] for field in q.fields}
        if any(value is not None and not isinstance(value, (str, int, float, bool))
               or isinstance(value, float) and not isfinite(value)
               for value in values.values()):
            raise QueryError("Requested employee fields contain invalid values")
        records.append(IndividualRecord(employee_number=employee, department=department,
                                        values=values))
    return records
