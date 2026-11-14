"""HR tool boundary: server identity → authorization → execution → release checks.

Both statistical and individual queries use the same handler, but have separate
permission grants and result checks. A rewrite never changes server data scope.
Only Gate's public view can reach the agent, and only after a successful audit or
aggregate release transaction. The demo memory-off path still enforces authorization,
minimum cell sizes and suppression; it deliberately skips disclosure history.
"""

from pydantic import TypeAdapter, ValidationError

from app import clock, gate
from app.gate import evidence
from app.memory import ledger, releases
from app.policy import store
from app.query import executor
from app.review import approvals
from app.schemas import Decision, Identity, IndividualQuery, StructuredQuery


TOOL_SPECS: list[dict] = [
    {"name": "query_hr", "description": "Request authorized HR statistics or individual fields.",
     "input_schema": {"type": "object", **TypeAdapter(StructuredQuery | IndividualQuery).json_schema()}},
]


def handle_query_hr(ctx: dict, args: dict) -> dict:
    """The agent sees only Gate's public view, after the audit/release write succeeds.

    Trusted context: who (Identity), project, session_id, optional dataset and memory.
    db/policy/now can be supplied by a server caller (and offline integration tests),
    never by tool arguments. The surrounding LLM/chat loop remains a separate concern.
    """
    try:
        query = (IndividualQuery.model_validate(args) if args.get("mode") == "individual"
                 else StructuredQuery.model_validate(args))
    except (ValidationError, AttributeError):
        return evidence.public_view(Decision("blocked", rule="L1.malformed",
                                             evidence={"rule": "L1.malformed"}))

    who = ctx.get("who")
    if not isinstance(who, Identity):
        return evidence.public_view(Decision("blocked", rule="L1.role",
                                             evidence={"role": "unknown", "metric": "query"}))
    try:
        if "db" in ctx:
            db = ctx["db"]
        else:
            from app.db import get_db
            db = get_db()
        policy = ctx["policy"] if "policy" in ctx else store.load_active(db, ctx.get("dataset", "ibm_hr"))
        now = ctx["now"] if "now" in ctx else clock.now()
        project, session_id = ctx["project"], ctx["session_id"]
        memory_on = ctx.get("memory", True)
        decision = gate.precheck(query, who, policy)
        if decision is None:
            if isinstance(query, IndividualQuery):
                records = executor.run_individual_query(db, query, now, who, policy)
                decision = gate.check_individual(query, who, records, policy)
            else:
                cells = executor.run_query(db, query, now, who, policy)
                targets = releases.release_targets(query, policy)
                history = releases.load_history(db, who, project, targets, now, policy) if memory_on else []
                updated_at = {}
                if memory_on and query.target is not None:
                    # Include ids that only occur in past releases: L4 needs those
                    # timestamps too. This metadata is server-only.
                    updated_at = {row["EmployeeNumber"]: row.get("updated_at", {})
                                  for row in db.employees.find({}, {"_id": 0, "EmployeeNumber": 1,
                                                                   "updated_at": 1})}
                decision = gate.check(query, who, cells, history, updated_at, policy)
        entry = {
            "kind": "gate_decision", "time": now, "user": who.user, "role": who.role,
            "team": who.team, "project": project, "session_id": session_id,
            "query": query.model_dump(), "status": decision.status,
            "memory_on": memory_on, "policy_version": policy["version"],
            **evidence.audit_view(decision),
        }
        if (isinstance(query, StructuredQuery) and memory_on
                and decision.status in ("allowed", "partial")):
            releases.record_then_release(db, decision, query, who, project, session_id,
                                         now, policy, entry)
        else:
            # Individual access is explicitly audited. It is never disguised as an
            # aggregate release and does not lower aggregate minimum group sizes.
            ledger.append(db, entry)
        return evidence.public_view(decision, viewer=who.user)
    except Exception:
        # Includes failed audit/transaction writes. No computed rows may escape.
        return evidence.public_view(Decision("blocked", rule="gate.error"))


def handle_request_review(ctx: dict, args: dict) -> dict:
    raise NotImplementedError
