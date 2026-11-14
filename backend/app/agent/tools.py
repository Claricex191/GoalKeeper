"""Tool definitions and handlers.

Contract with agent.loop:
  ctx = {"db", "who": Identity, "project", "session_id", "memory": bool, "policy",
         "now": datetime (optional; defaults to clock.now(), tests pass it explicitly)}
  tool_specs(policy, role, db=None) -> Anthropic tool definitions (strict)
  DISPATCH[name](ctx, tool_input) -> {"for_llm": dict, "trace": dict}
    for_llm: json.dumps into the tool_result content. The ONLY thing the LLM sees.
    trace:   appended to ChatResponse.trace (frontend/API.md, TraceEntry).

query_hr(query: StructuredQuery)
  1. validate (pydantic) → error status + message so the LLM can retry
  2. gate.precheck (L1) BEFORE touching data; rejected → ledger + reason
  3. cells = executor.run_query(db, query, now)          (QueryError → message)
  4. memory on:  history    = releases.load_history(...) for release_targets(query)
                 updated_at = target's last-modified time for EVERY employee that has
                              one (an employee who LEFT the group is not in `cells`)
     memory off: history = [], updated_at = {}  (control panel: no memory read)
  5. decision = gate.check(...)
  6. allowed / partial →
       memory on:  releases.record_then_release(...): releases + ledger in ONE
                   transaction; only then are the cells returned (fail closed)
       memory off: ledger only (memory_on False); nothing remembered
     blocked → ledger; evidence text, allowed_transforms, precedent hints

Neither view ever contains employee ids or vmin / vmax: the LLM gets
gate.evidence.public_view, the ledger gets gate.evidence.audit_view.
"""

import logging
from datetime import datetime

from pydantic import ValidationError

from app import clock, gate
from app.gate import evidence
from app.memory import ledger, precedents, releases
from app.policy import store
from app.query import executor
from app.schemas import Decision, StructuredQuery

log = logging.getLogger(__name__)

HIDDEN_REASON = "hidden to protect privacy"      # public: never say WHY (see evidence.py)
TRANSFORM_HELP = {
    "T1_merge_groups": "use fewer or coarser groups (drop a group_by column)",
    "T2_drop_filter": "remove one filter so the group is larger",
    "T3_widen_scope": "ask about a wider population, e.g. the whole department",
    "T4_extreme_to_avg": "ask for the average by group instead of the max / min",
}
_VALUE_HINT_LIMIT = 12
_value_cache: dict[str, list] = {}


# --- tool definitions -----------------------------------------------------------------
def _value_hints(db, columns: list[str]) -> str:
    """Exact spellings of categorical values, so the LLM writes 'Research & Development'
    and not 'R&D'. Columns with many values (e.g. YearsAtCompany) are left out."""
    lines = []
    for col in columns:
        if col not in _value_cache:
            _value_cache[col] = sorted(v for v in db.employees.distinct(col) if v is not None)
        vals = _value_cache[col]
        if 0 < len(vals) <= _VALUE_HINT_LIMIT:
            lines.append(f"- {col}: {', '.join(map(str, vals))}")
    return "\n".join(lines)


def tool_specs(policy: dict, role: str, db=None) -> list[dict]:
    description = (
        "Run one aggregate query over the HR dataset (employees active at the current "
        "date). Returns per-group head counts and values, or a refusal from the privacy "
        "gate with the reason and which rewrites are allowed. You cannot see individual "
        "records. If a query is blocked, you may retry ONCE per allowed transform: build "
        "the rewritten query yourself and set `transform` to the one you applied.")
    if db is not None:
        hints = _value_hints(db, list(policy["quasi_identifiers"]))
        if hints:
            description += "\n\nExact values:\n" + hints
    return [{
        "name": "query_hr",
        "description": description,
        "input_schema": store.tool_schema(policy, role),
        "strict": True,
    }]


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
    """Reviewer precedents (semantic memory) + plain-language allowed rewrites.
    Precedents only suggest; the gate re-checks every rewrite."""
    hints = []
    find = getattr(precedents, "find_similar", None)
    if find is not None:
        try:
            for p in find(ctx["db"], query, ctx["project"], k=3):
                t = p.get("transform")
                hints.append(f"Reviewer precedent: {p.get('label', '')}"
                             + (f" → {TRANSFORM_HELP.get(t, t)}" if t else ""))
        except NotImplementedError:
            pass
        except Exception as exc:                       # semantic memory is advisory
            log.warning("precedent search failed: %s", exc)
    return hints


def _remember_block(ctx, query: StructuredQuery, decision: Decision, now) -> None:
    rec = getattr(precedents, "record", None)
    if rec is None:
        return
    try:
        rec(ctx["db"], decision, query, ctx["who"], ctx["project"], now)
    except NotImplementedError:
        pass
    except Exception as exc:
        log.warning("precedent record failed: %s", exc)


def _ledger_entry(ctx, query: dict, status: str, now: datetime,
                  audit: dict | None = None) -> dict:
    who = ctx["who"]
    entry = {
        "kind": "gate_decision", "time": now,
        "user": who.user, "role": who.role, "team": who.team,
        "project": ctx["project"], "session_id": ctx["session_id"],
        "memory_on": bool(ctx["memory"]), "query": query, "status": status,
        "policy_version": int(ctx["policy"]["version"]),
    }
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
    trace = {
        "tool": "query_hr", "memory_on": bool(ctx["memory"]), "query": query,
        "status": status, "rule": public.get("rule"), "evidence_text": text,
        "cells": cells, "suppressed": suppressed,
        "allowed_transforms": transforms, "hints": hints or [],
    }
    for_llm = {"status": status, "cells": cells}
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
    db, who, policy = ctx["db"], ctx["who"], ctx["policy"]
    now = ctx.get("now") or clock.now()

    # 1. validate
    try:
        query = StructuredQuery.model_validate(args)
    except ValidationError as exc:
        msg = "Invalid query: " + "; ".join(e["msg"] for e in exc.errors()[:3])
        ledger.append(db, _ledger_entry(ctx, dict(args), "error", now) | {"rule": "invalid"})
        return _result(ctx, dict(args), "error", message=msg)
    qdict = query.model_dump()

    # 2. L1 before touching data
    pre = gate.precheck(query, who.role, policy)
    if pre is not None:
        return _blocked(ctx, query, qdict, pre, now)

    # 3. executor
    try:
        cells = executor.run_query(db, query, now)
    except executor.QueryError as exc:
        ledger.append(db, _ledger_entry(ctx, qdict, "error", now) | {"rule": "query_error"})
        return _result(ctx, qdict, "error", message=str(exc))

    # 4. memory (read only when memory is on)
    if ctx["memory"]:
        targets = releases.release_targets(query, policy)
        history = releases.load_history(db, who, ctx["project"], targets, now, policy)
        updated_at = _updated_at(db, query.target) if query.target else {}
    else:
        history, updated_at = [], {}

    # 5. gate
    decision = gate.check(query, who.role, cells, history, updated_at, policy)
    if decision.status == "blocked":
        return _blocked(ctx, query, qdict, decision, now)

    # 6. record first, then release (fail closed)
    entry = _ledger_entry(ctx, qdict, decision.status, now, evidence.audit_view(decision))
    if ctx["memory"]:
        releases.record_then_release(db, decision, query, who, ctx["project"],
                                     ctx["session_id"], now, policy, entry)
    else:
        ledger.append(db, entry)
    return _result(ctx, qdict, decision.status,
                   public=evidence.public_view(decision, viewer=who.user))


def _blocked(ctx, query: StructuredQuery, qdict: dict, decision: Decision, now) -> dict:
    ledger.append(ctx["db"], _ledger_entry(ctx, qdict, "blocked", now,
                                           evidence.audit_view(decision)))
    hints = _hints(ctx, query, decision)
    if decision.rule != "gate.error":
        _remember_block(ctx, query, decision, now)
    return _result(ctx, qdict, "blocked",
                   public=evidence.public_view(decision, viewer=ctx["who"].user), hints=hints)


DISPATCH = {"query_hr": handle_query_hr}
