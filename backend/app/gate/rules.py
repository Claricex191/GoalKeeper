"""L1–L3: checks that only look at the current query and its cells.

Rule ids (used in Decision.rule, suppressed[].reason, the ledger and the tests):
  L1.identifier    an identifier (policy never_return, or labelled "identifier") is used
                   anywhere: filter, group_by or target (e.g. EmployeeNumber = 1306)
  L1.unlabelled    a column the active policy has not labelled (default deny)
  L1.filter_field  a filter / group_by column that is not an approved quasi-identifier.
                   Threshold filters on salaries and ratings remain prohibited; an
                   aggregate grant does not authorize every query construction.
  L1.malformed     count with a target, or sum / avg / max / min without one
  L1.role          role lacks the requested category/operation (unknown role → deny)
  L1.scope         missing assignment or data outside the authorized department
  L1.metric        statistic is not applicable to the target field
  L1.extreme       max / min of a sensitive target: always one person's exact value,
                   whatever the group size
  L2.min_n         cell with fewer than k people (counts too)
  L2.homogeneous   everyone in the cell has the same target value (vmin == vmax)
  L3.secondary     extra cell hidden so subtraction from a total does not isolate
                   one protected group or a group with fewer than k people
"""
from math import isfinite

from app.policy.access import authorized_departments, permits_field
from app.schemas import Cell, Identity, IndividualQuery, StructuredQuery

EXTREME_METRICS = {"max", "min"}


def field_policy(query: StructuredQuery | IndividualQuery, who: Identity,
                 policy: dict) -> dict | None:
    """Authorize every field and operation before any database read.

    Query filters may narrow server-owned scope, never replace it. A business grant
    is only permission to compute; aggregate results still need L2–L4 approval.
    """
    role = who.role if isinstance(who, Identity) else "unknown"
    individual = isinstance(query, IndividualQuery)
    mode = "individual" if individual else "aggre_stats"
    metric = "individual" if individual else query.metric
    target = None if individual else query.target
    labels = policy["field_labels"]
    never = set(policy.get("never_return", []))
    quasi = list(policy["quasi_identifiers"])

    def violation(rule: str, fields=()) -> dict:
        allowed = [c for c in quasi if isinstance(who, Identity)
                   and permits_field(who, policy, c, mode)]
        return {"rule": rule, "fields": sorted(set(fields)), "role": role,
                "metric": metric, "target": target, "allowed_fields": allowed}

    if (not isinstance(who, Identity) or policy.get("schema_version") != 2
            or role not in policy["roles"]
            or not any(mode in modes for modes in policy["roles"][role].get("grants", {}).values())):
        return violation("L1.role")
    try:
        departments = authorized_departments(who, policy)
    except PermissionError:
        return violation("L1.scope")

    grouping = [] if individual else [f.col for f in query.filters] + list(query.group_by)
    columns = query.fields if individual else grouping + ([target] if target else [])
    bad = [c for c in columns if c in never or labels.get(c) == "identifier"]
    if bad:
        return violation("L1.identifier", bad)
    bad = [c for c in columns if c not in labels or c not in policy["field_categories"]]
    if bad:
        return violation("L1.unlabelled", bad)
    bad = [c for c in columns if not permits_field(who, policy, c, mode)]
    if bad:
        return violation("L1.role", bad)
    if individual:
        return None

    bad = [c for c in grouping if c not in quasi]
    if bad:
        return violation("L1.filter_field", bad)
    if (metric == "count") != (target is None):
        return violation("L1.malformed")
    # A targetless count is an employee/basic-information statistic, not a free
    # capability inherited from having aggregate access to some unrelated category.
    if metric == "count" and "aggre_stats" not in policy["roles"][role]["grants"].get("other_basic", []):
        return violation("L1.role")
    if departments is not None:
        for filt in query.filters:
            if filt.col != "Department":
                continue
            if filt.op == "=" and filt.val not in departments:
                return violation("L1.scope", ["Department"])
            if filt.op == "in" and (not isinstance(filt.val, list)
                                    or not set(filt.val).issubset(departments)):
                return violation("L1.scope", ["Department"])
    if metric in EXTREME_METRICS:
        return violation("L1.extreme")
    if target is not None and metric not in policy["field_metrics"].get(target, []):
        return violation("L1.metric", [target])
    return None


def effective_k(query: StructuredQuery, policy: dict) -> int:
    """The five-person floor cannot be lowered by a policy or an individual grant."""
    k = max(5, int(policy["k_threshold"]))
    return max(k, 10, int(policy.get("median_threshold", 10))) if query.metric == "median" else k


def check_cell_scope(cells: list[Cell], who: Identity, policy: dict) -> dict | None:
    departments = authorized_departments(who, policy)
    if departments is not None and any(
        not c.departments or not c.departments.issubset(departments) for c in cells
    ):
        return {"rule": "L1.scope", "role": who.role}
    return None


def cell_checks(cells: list[Cell], query: StructuredQuery,
                policy: dict) -> tuple[list[Cell], list[dict]]:
    """L2. Per cell:
      n < k                               → L2.min_n (counts too)
      target query and vmin == vmax       → L2.homogeneous: an average rating of 3.0 over
                                            ratings that are all 3 tells everyone's rating
    Return (kept, suppressed[{key, reason, n}]). `n` stays inside the gate and the audit
    view; the public view shows only the hidden keys.

    A target query whose cells lack vmin / vmax raises, so check() blocks it (fail closed).
    Not implemented: dominance (one person > dominance_share of the total).
    """
    k = effective_k(query, policy)
    kept, suppressed = [], []
    for cell in cells:
        if cell.n != len(cell.ids) or cell.n < 0:
            raise ValueError("cell population count does not match its employee ids")
        if query.target is not None and (cell.value is None or not isfinite(cell.value)):
            raise ValueError("target result must be finite")
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
    if not isfinite(cell.vmin) or not isfinite(cell.vmax) or cell.vmin > cell.vmax:
        raise ValueError("target bounds must be finite and ordered")
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
