"""The privacy gate. Pure functions: no database, no LLM, no clock.

Owner: Wenqian. Can be written and tested offline against the CSV.
Rule ids and what each rule does: rules.py (L1–L3) and cross_query.py (L4).

How the caller (agent.tools.handle_query_hr) uses it
  1. precheck(query, who, policy)   BEFORE the executor. Not None → blocked: do not run
                                     the query (so max / min and aggregate identifier filters
                                     never reach Atlas, and the executor needs no max/min)
  2. scoped executor (who, policy) → cells; for target queries every cell must carry vmin / vmax
  3. check(query, who, cells, history, updated_at, policy)
  4. evidence.public_view(decision, viewer=user) → the ONLY thing given to the LLM / trace
     evidence.audit_view(decision)               → ledger and reviewer
  5. allowed / partial with query.target set → record decision.cells as releases.
     Counts involving sensitive filter/group fields also require disclosure history.

Status
  blocked  an L1 or L4 hit, every cell hidden by L2 / L3, or any exception (fail closed)
  partial  some cells hidden; the rest are safe to return
  allowed  nothing hidden

allowed_transforms: what the agent may try after a block
  L1.identifier / unlabelled / filter_field / malformed / role / scope / metric → none: a permission
                                   problem, rephrasing cannot fix it (no review either)
  L1.extreme                     → T4_extreme_to_avg (e.g. the average by JobLevel)
  L2 / L3 (all hidden), L4       → T1_merge_groups, T2_drop_filter, T3_widen_scope
  partial                        → none: show what is safe and say what is hidden
The transform label on a rewritten query is for audit only: each rewrite goes through
precheck + check again like any other query.
"""

from app.gate import cross_query, rules
from app.policy.access import authorized_departments
from app.schemas import (Cell, Decision, Identity, IndividualQuery, IndividualRecord,
                         Release, StructuredQuery, Transform, UpdatedAt)

REWRITES: list[Transform] = ["T1_merge_groups", "T2_drop_filter", "T3_widen_scope"]
TRANSFORMS: dict[str, list[Transform]] = {
    "L1.extreme": ["T4_extreme_to_avg"],
    "L2.min_n": REWRITES, "L2.homogeneous": REWRITES, "L3.secondary": REWRITES,
    "L4.pairwise": REWRITES, "L4.linear": REWRITES, "L4.history": REWRITES,
    "L4.nonlinear": REWRITES,
}
HIDING_ORDER = ["L2.min_n", "L2.homogeneous", "L3.secondary"]


def precheck(query: StructuredQuery | IndividualQuery, who: Identity,
             policy: dict) -> Decision | None:
    """L1 only, before the executor. None → go ahead and run the query."""
    try:
        evidence = rules.field_policy(query, who, policy)
    except Exception as exc:
        return _error(exc)
    return None if evidence is None else _blocked(evidence)


def check(query: StructuredQuery, who: Identity, cells: list[Cell],
          history: list[Release], updated_at: UpdatedAt, policy: dict) -> Decision:
    """Run L1 → L2 → L3 → L4 in order.

    L1 rules.field_policy        reject the whole query (repeated from precheck, so a
                                 caller that skips precheck still fails closed)
    L2 rules.cell_checks         hide single cells: n < k, or everyone has the same value
    L3 rules.complementary       hide more cells until the hidden ones cannot be
                                 recovered by subtraction from a total
    L4 cross_query.check_history block the whole query, with evidence, for numeric
                                 targets or sensitive categorical counts. Ambiguous versions block before
                                 inference. Pairwise counts membership changes (A) and value
                                 changes since each old release (B); linear catches
                                 combinations of three or more answers.
    Hidden cells never reach L4 and are never recorded.
    Any exception → blocked with rule "gate.error" (fail closed).

    history: the team's releases for all disclosed targets ([] when memory is off).
    updated_at: last-modified time of query.target for EVERY employee that has one (not
    only the ids in `cells`), read from employees.updated_at by the caller so the gate
    stays free of database access. {} when memory is off.
    """
    try:
        return _check(query, who, cells, history, updated_at, policy)
    except Exception as exc:
        return _error(exc)


def _check(query: StructuredQuery, who: Identity, cells: list[Cell], history: list[Release],
           updated_at: UpdatedAt, policy: dict) -> Decision:
    if not isinstance(query, StructuredQuery):
        raise ValueError("individual queries require check_individual")
    evidence = rules.field_policy(query, who, policy)
    if evidence is not None:
        return _blocked(evidence)
    evidence = rules.check_cell_scope(cells, who, policy)
    if evidence is not None:
        return _blocked(evidence)
    if any(set(c.key) != set(query.group_by) for c in cells):
        raise ValueError("result group keys do not match the authorized query")
    k = rules.effective_k(query, policy)
    kept, hidden = rules.cell_checks(cells, query, policy)
    kept, hidden = rules.complementary(kept, hidden, query, k)
    if hidden and not kept:
        rule = _hiding_rule(hidden)
        return Decision("blocked", suppressed=hidden, rule=rule, evidence={"rule": rule},
                        allowed_transforms=list(TRANSFORMS[rule]))
    if query.target is not None:
        evidence = cross_query.check_history(kept, history, query.target, updated_at, k,
                                             metric=query.metric)
        if evidence is not None:
            return _blocked(evidence)
    else:
        # Counts over sensitive categories (e.g. AgeBand) are categorical
        # disclosures, not equations summing an employee's age or salary.
        fields = {f.col for f in query.filters} | set(query.group_by)
        for target in sorted(fields & set(policy["sensitive_targets"])):
            evidence = cross_query.check_history(kept, history, target, updated_at, k,
                                                 metric="count")
            if evidence is not None:
                return _blocked(evidence)
    if hidden:
        rule = _hiding_rule(hidden)
        return Decision("partial", cells=kept, suppressed=hidden, rule=rule,
                        evidence={"rule": rule})
    return Decision("allowed", cells=kept)


def check_individual(query: IndividualQuery, who: Identity,
                     records: list[IndividualRecord], policy: dict) -> Decision:
    """Release only explicitly selected, authorized fields from scoped records.

    An individual grant is independent of aggregate grants. These records are audited
    as individual access; they must not be stored as one-person aggregate releases.
    """
    try:
        if not isinstance(query, IndividualQuery):
            raise ValueError("expected an individual query")
        evidence = rules.field_policy(query, who, policy)
        if evidence is not None:
            return _blocked(evidence)
        departments = authorized_departments(who, policy)
        requested = set(query.employee_numbers)
        seen = set()
        for record in records:
            if (record.employee_number not in requested
                    or (departments is not None and record.department not in departments)):
                return _blocked({"rule": "L1.scope", "role": who.role})
            if record.employee_number in seen or set(record.values) != set(query.fields):
                raise ValueError("individual response does not match the requested projection")
            seen.add(record.employee_number)
        indices = {employee: index for index, employee in enumerate(query.employee_numbers)}
        return Decision("allowed", records=records, mode="individual",
                        record_indices=[indices[record.employee_number] for record in records])
    except Exception as exc:
        return _error(exc)


def _blocked(evidence: dict) -> Decision:
    rule = evidence["rule"]
    return Decision("blocked", evidence=evidence, rule=rule,
                    allowed_transforms=list(TRANSFORMS.get(rule, [])))


def _error(exc: Exception) -> Decision:
    return Decision("blocked", rule="gate.error",
                    evidence={"rule": "gate.error", "error": f"{type(exc).__name__}: {exc}"})


def _hiding_rule(hidden: list[dict]) -> str:
    reasons = {s["reason"] for s in hidden}
    return next(r for r in HIDING_ORDER if r in reasons)
