"""Turn a StructuredQuery into one Atlas aggregation and return cells.

One generic function covers every supported question. No raw-record access, no joins.
Results stay on the server until the gate has decided.
"""
from datetime import datetime

from app.schemas import Cell, StructuredQuery

OPS = {"=": "$eq", "!=": "$ne", "<": "$lt", "<=": "$lte",
       ">": "$gt", ">=": "$gte", "in": "$in"}


def build_pipeline(q: StructuredQuery, now: datetime) -> list[dict]:
    """$match (filters merged per column, plus active == True and start_date <= now)
    → $group by q.group_by with n, ids ($push EmployeeNumber), value ($avg/$sum on target).
    `now` is clock.now() (simulated), so advancing the demo clock past a new hire's
    start_date adds them to every population."""
    raise NotImplementedError


def run_query(db, q: StructuredQuery, now: datetime) -> list[Cell]:
    raise NotImplementedError
