"""The privacy gate. Pure functions: no database, no LLM, no clock.

Owner: Wenqian. Can be written and tested offline against the CSV.
Rule ids and what each rule does: rules.py (L1–L3) and cross_query.py (L4).

How the caller (agent.tools.handle_query_hr) uses it
  1. precheck(query, role, policy)   BEFORE the executor. Not None → blocked: do not run
                                     the query (so max / min and EmployeeNumber queries
                                     never reach Atlas, and the executor needs no max/min)
  2. executor → cells; for target queries every cell must carry vmin / vmax
  3. check(query, role, cells, history, updated_at, policy)
  4. evidence.public_view(decision, viewer=user) → the ONLY thing given to the LLM / trace
     evidence.audit_view(decision)               → ledger and reviewer
  5. allowed / partial with query.target set → record decision.cells as releases.
     Counts are never recorded: they reveal no sensitive value.

Status
  blocked  an L1 or L4 hit, every cell hidden by L2 / L3, or any exception (fail closed)
  partial  some cells hidden; the rest are safe to return
  allowed  nothing hidden

allowed_transforms: what the agent may try after a block
  L1.identifier / unlabelled / filter_field / malformed / role → none: a permission
                                   problem, rephrasing cannot fix it (no review either)
  L1.extreme                     → T4_extreme_to_avg (e.g. the average by JobLevel)
  L2 / L3 (all hidden), L4       → T1_merge_groups, T2_drop_filter, T3_widen_scope
  partial                        → none: show what is safe and say what is hidden
The transform label on a rewritten query is for audit only: each rewrite goes through
precheck + check again like any other query.
"""

from app.gate import cross_query, rules
from app.schemas import Cell, Decision, Release, StructuredQuery, Transform, UpdatedAt

REWRITES: list[Transform] = ["T1_merge_groups", "T2_drop_filter", "T3_widen_scope"]
TRANSFORMS: dict[str, list[Transform]] = {
    "L1.extreme": ["T4_extreme_to_avg"],
    "L2.min_n": REWRITES, "L2.homogeneous": REWRITES, "L3.secondary": REWRITES,
    "L4.pairwise": REWRITES, "L4.linear": REWRITES,
}
HIDING_ORDER = ["L2.min_n", "L2.homogeneous", "L3.secondary"]


def precheck(query: StructuredQuery, role: str, policy: dict) -> Decision | None:
    """L1 only, before the executor. None → go ahead and run the query."""
    try:
        evidence = rules.field_policy(query, role, policy)
    except Exception as exc:
        return _error(exc)
    return None if evidence is None else _blocked(evidence)


def check(query: StructuredQuery, role: str, cells: list[Cell],
          history: list[Release], updated_at: UpdatedAt, policy: dict) -> Decision:
    """Run L1 → L2 → L3 → L4 in order.

    L1 rules.field_policy        reject the whole query (repeated from precheck, so a
                                 caller that skips precheck still fails closed)
    L2 rules.cell_checks         hide single cells: n < k, or everyone has the same value
    L3 rules.complementary       hide more cells until the hidden ones cannot be
                                 recovered by subtraction from a total
    L4 cross_query.check_history block the whole query, with evidence; target queries
                                 only. Pairwise counts membership changes (A) and value
                                 changes since each old release (B); linear catches
                                 combinations of three or more answers.
    Hidden cells never reach L4 and are never recorded.
    Any exception → blocked with rule "gate.error" (fail closed).

    history: the team's releases for query.target ([] when memory is off).
    updated_at: last-modified time of query.target for EVERY employee that has one (not
    only the ids in `cells`), read from employees.updated_at by the caller so the gate
    stays free of database access. {} when memory is off.
    """
    try:
        return _check(query, role, cells, history, updated_at, policy)
    except Exception as exc:
        return _error(exc)


def _check(query: StructuredQuery, role: str, cells: list[Cell], history: list[Release],
           updated_at: UpdatedAt, policy: dict) -> Decision:
    evidence = rules.field_policy(query, role, policy)
    if evidence is not None:
        return _blocked(evidence)
    k = policy["k_threshold"]
    kept, hidden = rules.cell_checks(cells, query, policy)
    kept, hidden = rules.complementary(kept, hidden, query, k)
    if hidden and not kept:
        rule = _hiding_rule(hidden)
        return Decision("blocked", suppressed=hidden, rule=rule, evidence={"rule": rule},
                        allowed_transforms=list(TRANSFORMS[rule]))
    if query.target is not None:
        evidence = cross_query.check_history(kept, history, query.target, updated_at, k)
        if evidence is not None:
            return _blocked(evidence)
    if hidden:
        rule = _hiding_rule(hidden)
        return Decision("partial", cells=kept, suppressed=hidden, rule=rule,
                        evidence={"rule": rule})
    return Decision("allowed", cells=kept)


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
