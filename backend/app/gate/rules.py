"""L1–L3: checks that only look at the current query."""
from app.schemas import Cell, StructuredQuery


def field_policy(query: StructuredQuery, role: str, policy: dict) -> str | None:
    """L1. Return a rule id if this role may not run this metric on this target, else None.
    Identifiers are never returnable; max/min on sensitive targets are never allowed.
    Default deny: any column in filters, group_by or target that does not appear in
    policy["field_labels"] (e.g. a newly added field not yet labelled and approved)
    → reject."""
    raise NotImplementedError


def cell_checks(cells: list[Cell], query: StructuredQuery, policy: dict) -> tuple[list[Cell], list[dict]]:
    """L2. Per cell: n < k; median needs n >= median_threshold;
    homogeneity (sensitive count == 0 or == n); optional dominance (top contributor share).
    Return (kept, suppressed[{key, reason}])."""
    raise NotImplementedError


def complementary(kept: list[Cell], suppressed: list[dict], query: StructuredQuery) -> tuple[list[Cell], list[dict]]:
    """L3. If a total is shown and exactly one cell under it is suppressed,
    suppress the next smallest cell too (or drop the total)."""
    raise NotImplementedError
