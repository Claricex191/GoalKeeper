"""HR tool boundary shared by the LLM loop, privacy gate and review workflow.

Trusted ctx: db, who (Identity), policy, project, session_id, memory, optional now
and turn_id. No identity or data scope is read from tool arguments.

Contract: tool_specs(policy, who, db=None) supplies role-filtered strict definitions;
DISPATCH[name](ctx, args) returns {for_llm, trace}. Only Gate-approved public data can
reach either view, after a successful audit/release write. Aggregate authorization
and L5 review are combined with v2 category grants, forced department scope and an
independent Individual query path. Reviews never override authorization.
"""

import logging
from datetime import datetime, timedelta

from pydantic import ValidationError

from app import clock, gate
from app.gate import evidence
from app.memory import ledger, precedents, releases
from app.policy import store
from app.query import executor
from app.review import approvals
from app.schemas import Decision, Identity, IndividualQuery, StructuredQuery
from app.policy.access import authorized_departments, permits_field

log = logging.getLogger(__name__)

HIDDEN_REASON = "hidden to protect privacy"      # public: never say WHY (see evidence.py)
TRANSFORM_HELP = {
    "T1_merge_groups": "use fewer or coarser groups (drop a group_by column)",
    "T2_drop_filter": "remove one filter so the group is larger",
    "T3_widen_scope": "ask about a wider population, e.g. the whole department",
    "T4_extreme_to_avg": "ask for the average by group instead of the max / min",
}
# --- tool definitions -----------------------------------------------------------------
def _value_hints(policy: dict, who: Identity) -> str:
    """Offer policy-declared vocabulary, never an un-gated read of employee values.

    Even a distinct value can disclose membership of a small group. Tool definitions
    therefore do not read employees or cache one user's observed values for another.
    """
    if not permits_field(who, policy, "Department", "aggre_stats"):
        return ""
    scope = authorized_departments(who, policy)
    values = list(scope) if scope is not None else list(policy["departments"])
    return "- Department: " + ", ".join(values) if values else ""


def tool_specs(policy: dict, who: Identity | str, db=None, now=None) -> list[dict]:
    """Role-filtered strict schemas; employee reads always require full Identity.

    A role string remains supported for static schema generation by older callers.
    It never authorizes a query or enables data-derived value hints. ``db``/``now``
    remain accepted for caller compatibility, but definitions do not access data.
    """
    if isinstance(who, Identity):
        role = who.role
        try:
            authorized_departments(who, policy)
        except (PermissionError, KeyError, TypeError):
            return []
    elif isinstance(who, str):
        role = who
    else:
        return []
    descriptions = {
        "aggre_stats": (
            "Run one aggregate query over employees in your authorized scope. "
            "Results may be withheld by privacy checks. After a block, you may retry "
            "once per allowed transform; set transform to the rewrite you applied. "
            "Rewrites cannot widen your authorized department scope."),
        "individual": (
            "Read explicitly authorized fields for requested employees within your "
            "authorized scope. Each result's request_index is its zero-based position "
            "in employee_numbers; absent or out-of-scope employees are omitted. "
            "Individual access does not relax privacy checks on aggregate queries."),
    }
    specs = []
    for mode, name in (("aggre_stats", "query_hr"), ("individual", "query_hr_individual")):
        try:
            schema = store.tool_schema(policy, role, mode=mode)
        except (PermissionError, ValueError, KeyError):
            continue
        description = descriptions[mode]
        if mode == "aggre_stats" and isinstance(who, Identity):
            hints = _value_hints(policy, who)
            if hints:
                description += "\n\nPolicy vocabulary within your scope:\n" + hints
        specs.append({"name": name, "description": description,
                      "input_schema": schema, "strict": True})
    return specs


# --- query_hr -------------------------------------------------------------------------
def _round(v):
    return None if v is None else round(float(v), 2)


def _updated_at(db, target: str) -> dict:
    """{EmployeeNumber: {target: datetime}} for every employee whose target changed."""
    out = {}
    for d in db.employees.find({f"updated_at.{target}": {"$exists": True}},
                               {"_id": 0, "EmployeeNumber": 1, "updated_at": 1}):
        out[int(d["EmployeeNumber"])] = {target: d["updated_at"][target]}
    return out


def _hints(ctx, query: StructuredQuery, decision: Decision) -> list[str]:
    """Similar earlier refusals from the semantic memory (precedents, Vector Search),
    with the rewrite that worked for them. Advisory: the gate re-checks every rewrite.
    Memory-off panel: no semantic memory either."""
    if not ctx["memory"]:
        return []
    hints = []
    try:
        for p in precedents.find_similar(ctx["db"], query, ctx["project"], k=3):
            t = p.get("transform")
            hints.append(f"Similar question refused before: {p['label']}"
                         + (f" → what worked: {TRANSFORM_HELP.get(t, t)}" if t else ""))
    except Exception as exc:                       # semantic memory is advisory
        log.warning("precedent search failed: %s", exc)
    return hints


def _precedent(fn, ctx, *args) -> None:
    """record / resolve a precedent; never let semantic memory break an answer."""
    if not ctx["memory"]:
        return
    try:
        fn(ctx["db"], *args)
    except Exception as exc:
        log.warning("precedent %s failed: %s", fn.__name__, exc)


def _ledger_entry(ctx, query: dict, status: str, now: datetime,
                  audit: dict | None = None) -> dict:
    who = ctx["who"]
    entry = {
        "kind": "gate_decision", "time": now,
        "user": who.user, "role": who.role, "team": who.team,
        "project": ctx["project"], "session_id": ctx["session_id"],
        "memory_on": bool(ctx.get("memory", True)), "query": query, "status": status,
        "policy_version": int(ctx["policy"]["version"]),
    }
    if ctx.get("turn_id"):
        entry["turn_id"] = ctx["turn_id"]
    if audit:
        entry.update({"rule": audit["rule"], "evidence_text": audit["evidence_text"],
                      "suppressed": audit["suppressed"]})
    if query.get("transform"):
        entry["transform"] = query["transform"]
    return entry


def _result(ctx, query: dict, status: str, *, public: dict | None = None,
            message: str | None = None, hints: list[str] | None = None) -> dict:
    public = public or {}
    cells = [{"group": c["key"], "n": c["n"], "value": _round(c["value"])}
             for c in public.get("cells", [])]
    suppressed = [{"group": k, "reason": HIDDEN_REASON} for k in public.get("hidden", [])]
    transforms = public.get("allowed_transforms", [])
    text = message or public.get("evidence_text")
    # Employee selectors were provided by the caller, but do not echo them into
    # the public trace. request_index maps results back to that input without ids.
    individual = query.get("mode") == "individual"
    selectors = query.get("employee_numbers")
    public_query = ({"mode": "individual", "fields": query.get("fields", []),
                     "requested_count": len(selectors) if isinstance(selectors, list) else 0}
                    if individual else query)
    trace = {
        "tool": "query_hr_individual" if individual else "query_hr",
        "memory_on": bool(ctx.get("memory", True)), "query": public_query,
        "status": status, "rule": public.get("rule"), "evidence_text": text,
        "cells": cells, "suppressed": suppressed,
        "allowed_transforms": transforms, "hints": hints or [],
    }
    for_llm = {"status": status, "cells": cells}
    if "records" in public:
        trace["records"] = public["records"]
        for_llm["records"] = public["records"]
    if suppressed:
        for_llm["hidden_groups"] = [s["group"] for s in suppressed]
    if text:
        for_llm["message"] = text
    if transforms:
        for_llm["allowed_transforms"] = {t: TRANSFORM_HELP.get(t, t) for t in transforms}
    if hints:
        for_llm["hints"] = hints
    return {"for_llm": for_llm, "trace": trace}


def handle_query_hr(ctx: dict, args: dict) -> dict:
    """Return main's for_llm/trace contract after v2 authorization and release writes."""
    qdict = dict(args) if isinstance(args, dict) else {}
    who = ctx.get("who")
    if not isinstance(who, Identity):
        decision = Decision("blocked", rule="L1.role",
                            evidence={"role": "unknown", "metric": "query"})
        return _result(ctx, qdict, "blocked", public=evidence.public_view(decision))
    try:
        trusted = dict(ctx)
        trusted.setdefault("memory", True)
        if "db" not in trusted:
            from app.db import get_db
            trusted["db"] = get_db()
        if "policy" not in trusted:
            trusted["policy"] = store.load_active(trusted["db"], trusted.get("dataset", "ibm_hr"))
        now = trusted["now"] if "now" in trusted else clock.now()
        db, policy = trusted["db"], trusted["policy"]
        try:
            model = IndividualQuery if qdict.get("mode") == "individual" else StructuredQuery
            query = model.model_validate(args)
        except ValidationError as exc:
            msg = "Invalid query: " + "; ".join(e["msg"] for e in exc.errors()[:3])
            ledger.append(db, _ledger_entry(trusted, qdict, "error", now) | {"rule": "invalid"})
            return _result(trusted, qdict, "error", message=msg)
        qdict = query.model_dump()

        # Authorization precedes review/probing: no approval may grant a role, field,
        # or department the caller lacks. Extremes are privacy refusals, so they can
        # still participate in the original L5 counting/review flow.
        pre = gate.precheck(query, who, policy)
        if pre is not None and pre.rule != "L1.extreme":
            return _blocked(trusted, query, qdict, pre, now)
        if isinstance(query, StructuredQuery):
            probe = _probing(trusted, query, qdict, now)
            if probe is not None:
                return probe
        if pre is not None:
            return _blocked(trusted, query, qdict, pre, now)

        try:
            if isinstance(query, IndividualQuery):
                records = executor.run_individual_query(db, query, now, who, policy)
                decision = gate.check_individual(query, who, records, policy)
            else:
                cells = executor.run_query(db, query, now, who, policy)
                if trusted["memory"]:
                    targets = releases.release_targets(query, policy)
                    history = releases.load_history(db, who, trusted["project"], targets, now, policy)
                    updated_at = _updated_at(db, query.target) if query.target else {}
                else:
                    history, updated_at = [], {}
                decision = gate.check(query, who, cells, history, updated_at, policy)
        except executor.QueryError as exc:
            ledger.append(db, _ledger_entry(trusted, qdict, "error", now) | {"rule": "query_error"})
            return _result(trusted, qdict, "error", message=str(exc))
        if decision.status == "blocked":
            return _blocked(trusted, query, qdict, decision, now)

        entry = _ledger_entry(trusted, qdict, decision.status, now, evidence.audit_view(decision))
        if isinstance(query, StructuredQuery) and trusted["memory"]:
            releases.record_then_release(db, decision, query, who, trusted["project"],
                                         trusted["session_id"], now, policy, entry)
        else:
            # Individual reads are audited without creating one-person aggregate
            # releases. All statistics still require the same minimum cohort size.
            ledger.append(db, entry)
        if isinstance(query, StructuredQuery) and query.transform:
            _precedent(precedents.resolve, trusted, query, who, trusted["project"], now)
        return _result(trusted, qdict, decision.status,
                       public=evidence.public_view(decision, viewer=who.user))
    except Exception as exc:
        # Includes failed audit/transaction/history reads. No calculated result escapes.
        log.warning("HR query failed closed (%s)", type(exc).__name__)
        return _result(ctx, qdict, "blocked",
                       public=evidence.public_view(Decision("blocked", rule="gate.error")))


def _blocked(ctx, query: StructuredQuery | IndividualQuery, qdict: dict, decision: Decision, now) -> dict:
    ledger.append(ctx["db"], _ledger_entry(ctx, qdict, "blocked", now,
                                           evidence.audit_view(decision)))
    retryable = isinstance(query, StructuredQuery) and bool(decision.allowed_transforms)
    hints = _hints(ctx, query, decision) if retryable else []
    if retryable and decision.rule != "gate.error":
        _precedent(precedents.record, ctx, decision, query, ctx["who"], ctx["project"], now)
    return _result(ctx, qdict, "blocked",
                   public=evidence.public_view(decision, viewer=ctx["who"].user), hints=hints)


# --- L5: repeated probing ---------------------------------------------------------------
L5_RULES = ["L5.probing", "L5.pending"]
L5_DEFAULTS = {"max_attempts": 3, "window_days": 7}   # policy["probing"] overrides


def _probing(ctx, query: StructuredQuery, qdict: dict, now) -> dict | None:
    """L5. After a user's FIRST block on a sensitive field, count that user's further
    questions (chat messages) on the same field, whatever their outcome. When a new
    message would be the (max_attempts + 1)-th (default 3 → the 4th), it goes to a reviewer
    instead of the data, and the field stays locked for that user until the reviewer
    decides; the decision restarts the count.

    A question = one user message (ctx["turn_id"], set by the loop): the agent's
    automatic rewrites inside one message count once and are never cut off midway.
    `transform` is NOT used to tell them apart: the LLM fills it, so it could be gamed.
    Scope: memory on, same user, project and target, across sessions, within
    window_days of demo time; errors and L5 entries are not counted. Ordered by ledger
    _id because the demo clock does not move within a day. Without a turn_id (tests,
    scripts) every call is its own question.
    """
    if not isinstance(query, StructuredQuery) or not ctx["memory"] or query.target is None:
        return None
    db, who = ctx["db"], ctx["who"]
    cfg = {**L5_DEFAULTS, **ctx["policy"].get("probing", {})}
    target = query.target

    if approvals.pending_for(db, who.user, ctx["project"], target):
        return _l5(ctx, qdict, now, "L5.pending",
                   f"Questions about {target} are paused until a reviewer has looked at "
                   f"your earlier request.")

    base = {"kind": "gate_decision", "user": who.user, "project": ctx["project"],
            "memory_on": True, "query.target": target,
            "rule": {"$nin": L5_RULES}, "status": {"$in": ["allowed", "partial", "blocked"]},
            "time": {"$gte": now - timedelta(days=int(cfg["window_days"]))}}
    review_reset = approvals.reset_point(db, who.user, ctx["project"], target)
    demo_reset = db.meta.find_one({"_id": "demo_reset"})
    cutoffs = [point for point in (review_reset,
               demo_reset.get("ledger_after") if demo_reset else None) if point is not None]
    if cutoffs:
        base["_id"] = {"$gt": max(cutoffs)}
    first_block = db.ledger.find_one({**base, "status": "blocked"}, sort=[("_id", 1)])
    if first_block is None:
        return None
    turn = ctx.get("turn_id")
    if turn and turn == first_block.get("turn_id"):
        return None                       # still the message that was first refused
    later = {d.get("turn_id") or d["_id"] for d in db.ledger.find(
        {**base, "_id": {"$gt": first_block["_id"]}}, {"turn_id": 1})}
    later.discard(first_block.get("turn_id"))
    if turn and turn in later:
        return None                       # a rewrite inside an already-counted message
    if len(later) < int(cfg["max_attempts"]):
        return None

    reason = (f"{len(later) + 1} more questions about {target} after an answer was "
              f"refused (limit {cfg['max_attempts']})")
    approval_id = approvals.create(db, who.user, who.team, ctx["project"], target, qdict,
                                   reason, now)
    return _l5(ctx, qdict, now, "L5.probing",
               f"You have asked about {target} several times since an answer was refused. "
               f"This request has been sent to a reviewer; questions about {target} are "
               f"paused until they decide.", approval_id=approval_id)


def _l5(ctx, qdict: dict, now, rule: str, text: str, approval_id: str | None = None) -> dict:
    entry = _ledger_entry(ctx, qdict, "blocked", now,
                          {"rule": rule, "evidence_text": text, "suppressed": []})
    if approval_id:
        entry["approval_id"] = approval_id
    ledger.append(ctx["db"], entry)
    out = _result(ctx, qdict, "blocked", public={"rule": rule, "evidence_text": text})
    if approval_id:
        out["trace"]["approval_id"] = approval_id
        out["for_llm"]["review"] = "pending"
    return out


def handle_individual_hr(ctx: dict, args: dict) -> dict:
    # Bind the dispatched tool to its mode even for direct/non-strict callers.
    # The shared handler revalidates all fields and rejects aggregate arguments.
    if isinstance(args, dict):
        args = {**args, "mode": "individual"}
    return handle_query_hr(ctx, args)


DISPATCH = {"query_hr": handle_query_hr, "query_hr_individual": handle_individual_hr}
