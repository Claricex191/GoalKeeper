"""L1–L3: checks that only look at the current query and its cells.

Rule ids (used in Decision.rule, suppressed[].reason, the ledger and the tests):
  L1.identifier    an identifier (policy never_return, or labelled "identifier") is used
                   anywhere: filter, group_by or target (e.g. EmployeeNumber = 1306)
  L1.unlabelled    a column the active policy has not labelled (default deny)
  L1.filter_field  a filter / group_by column that is not a quasi-identifier. Sensitive
                   fields may only be the target: "count where MonthlyIncome > X" would
                   let anyone close in on a salary, and counts are not checked by L4
  L1.malformed     count with a target, or sum / avg / max / min without one
  L1.role          this role may not use this target or metric (unknown role → deny)
  L1.extreme       max / min of a sensitive target: always one person's exact value,
                   whatever the group size
  L2.min_n         cell with fewer than k people (counts too)
  L2.homogeneous   everyone in the cell has the same target value (vmin == vmax)
  L3.secondary     extra cell hidden so subtraction from a total does not isolate
                   one protected group or a group with fewer than k people
"""
from app.schemas import Cell, StructuredQuery

EXTREME_METRICS = {"max", "min"}


def field_policy(query: StructuredQuery, role: str, policy: dict) -> dict | None:
    """L1. Return evidence for the first violation, else None.

    Looks at the query only, so it runs before the executor (gate.precheck): a rejected
    query never reaches the database. Evidence:
      {"rule", "fields" (offending columns), "role", "metric", "target", "allowed_fields"}
    """
    labels = policy["field_labels"]
    never = set(policy.get("never_return", []))
    quasi = list(policy["quasi_identifiers"])
    grouping = [f.col for f in query.filters] + list(query.group_by)
    columns = grouping + ([query.target] if query.target is not None else [])

    def violation(rule: str, fields: list[str] | tuple = ()) -> dict:
        return {"rule": rule, "fields": sorted(set(fields)), "role": role,
                "metric": query.metric, "target": query.target, "allowed_fields": quasi}

    bad = [c for c in columns if c in never or labels.get(c) == "identifier"]
    if bad:
        return violation("L1.identifier", bad)
    bad = [c for c in columns if c not in labels]
    if bad:
        return violation("L1.unlabelled", bad)
    bad = [c for c in grouping if c not in quasi]
    if bad:
        return violation("L1.filter_field", bad)
    if (query.metric == "count") != (query.target is None):
        return violation("L1.malformed")
    perms = policy["roles"].get(role)
    if perms is None:
        return violation("L1.role")
    if query.target is not None and query.target not in role_targets(perms, policy):
        return violation("L1.role")
    if query.metric in EXTREME_METRICS:
        return violation("L1.extreme")
    if query.metric not in perms["metrics"]:
        return violation("L1.role")
    return None


def role_targets(perms: dict, policy: dict) -> set[str]:
    """Targets a role may query. "*" means every sensitive target. A field that is not a
    sensitive target is never a target (v1), even if a role lists it."""
    sensitive = set(policy["sensitive_targets"])
    return sensitive if "*" in perms["targets"] else sensitive & set(perms["targets"])


def cell_checks(cells: list[Cell], query: StructuredQuery,
                policy: dict) -> tuple[list[Cell], list[dict]]:
    """L2. Per cell:
      n < k                               → L2.min_n (counts too)
      target query and vmin == vmax       → L2.homogeneous: an average rating of 3.0 over
                                            ratings that are all 3 tells everyone's rating
    Return (kept, suppressed[{key, reason, n}]). `n` stays inside the gate and the audit
    view; the public view shows only the hidden keys.

    A target query whose cells lack vmin / vmax raises, so check() blocks it (fail closed).
    Not implemented: dominance (one person > dominance_share of the total), median.
    """
    k = policy["k_threshold"]
    kept, suppressed = [], []
    for cell in cells:
        if cell.n < k:
            reason = "L2.min_n"
        elif query.target is not None and _homogeneous(cell):
            reason = "L2.homogeneous"
        else:
            kept.append(cell)
            continue
        suppressed.append({"key": cell.key, "reason": reason, "n": cell.n})
    return kept, suppressed


def _homogeneous(cell: Cell) -> bool:
    if cell.vmin is None or cell.vmax is None:
        raise ValueError(f"cell {cell.key} has no vmin/vmax; the executor must supply "
                         "them for every target query")
    return cell.vmin == cell.vmax


def complementary(kept: list[Cell], suppressed: list[dict], query: StructuredQuery,
                  k: int) -> tuple[list[Cell], list[dict]]:
    """L3. Protect hidden cells against subtraction from totals of the same population.

    Any homogeneous remainder can reveal every value in it, even when its count is
    above k. Hiding one other group can still leave only one possible value for the
    homogeneous group when the target has a known, narrow range. Row and column
    totals add constraints for two-dimensional tables. When L2 flags a homogeneous
    cell, suppress the grouped answer so it adds no aggregates.

    For small cells, each margin (whole answer and, for two dimensions, rows and
    columns) must hide 0 or >= k people. Add the smallest visible cell in an unsafe
    margin and repeat. These rules cover subtraction using this answer's cells and
    totals. Other overlapping queries and bounds on possible values can add information.
    """
    kept, suppressed = list(kept), list(suppressed)
    dims = list(query.group_by)
    if any(s["reason"] == "L2.homogeneous" for s in suppressed):
        suppressed.extend({"key": cell.key, "reason": "L3.secondary", "n": cell.n}
                          for cell in kept)
        return [], suppressed
    margins = ([[d] for d in dims] + [[]]) if len(dims) == 2 else [[]]
    while True:
        extra = _secondary_candidate(kept, suppressed, margins, k)
        if extra is None:
            return kept, suppressed
        kept = [c for c in kept if c is not extra]
        suppressed.append({"key": extra.key, "reason": "L3.secondary", "n": extra.n})


def _secondary_candidate(kept: list[Cell], suppressed: list[dict],
                         margins: list[list[str]], k: int) -> Cell | None:
    for dims in margins:
        hidden_n: dict[tuple, int] = {}
        for s in suppressed:
            part = _part(s["key"], dims)
            hidden_n[part] = hidden_n.get(part, 0) + s["n"]
        shown: dict[tuple, list[Cell]] = {}
        for c in kept:
            shown.setdefault(_part(c.key, dims), []).append(c)
        for part in sorted(hidden_n, key=repr):
            if 0 < hidden_n[part] < k and shown.get(part):
                return min(shown[part], key=lambda c: (c.n, repr(sorted(c.key.items()))))
    return None


def _part(key: dict, dims: list[str]) -> tuple:
    return tuple(key.get(d) for d in dims)
