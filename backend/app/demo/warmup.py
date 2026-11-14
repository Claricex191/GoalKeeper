"""Seed the two Monday disclosures through the executor and privacy gate.

This is shared by the reset endpoint and the CLI. It never calls an LLM and never
invents employee populations or statistics: both come from the current database.
"""

from app import clock, gate
from app.demo.state import operation_lock
from app.gate import evidence
from app.memory import releases
from app.policy import store
from app.query import executor
from app.schemas import StructuredQuery
from app.users import DEMO_USERS

DATASET = "ibm_hr"
PROJECT = "comp-review-2026"
SESSION_ID = "warmup"
SCENARIOS = (("mgr_1", "Sales"), ("mgr_4", "Human Resources"))


class WarmupError(ValueError):
    """Warmup needs a clean Monday state and two fully allowed answers."""


def _run(db, session) -> int:
    # Serialize standalone scripts and API processes on the shared clock document.
    # The process lock alone cannot stop two independent CLI invocations from both
    # observing an empty release collection and appending duplicate baselines.
    db.meta.update_one({"_id": "clock"},
                       {"$inc": {"warmup_generation": 1},
                        "$setOnInsert": {"offset_days": 0.0}},
                       upsert=True, session=session)
    if clock.offset_days(db=db, session=session) != 0:
        raise WarmupError("warmup requires Monday; use /admin/reset to start over")
    now = clock.now(db=db, session=session)
    if db.releases.find_one({"project": PROJECT}, {"_id": 1}, session=session):
        raise WarmupError("warmup already has project history; use /admin/reset")
    if db.employees.find_one({}, {"_id": 1}, session=session) is None:
        raise LookupError("employees is empty: run scripts/load_data.py")
    dirty = {"updated_at": {"$exists": True, "$ne": {}}}
    if db.employees.find_one(dirty, {"_id": 1}, session=session):
        raise WarmupError("warmup requires unmodified employees; use /admin/reset")

    policy = store.load_active(db, DATASET, session=session)
    prepared = []
    for user, department in SCENARIOS:
        who = DEMO_USERS[user]
        query = StructuredQuery(
            filters=[{"col": "Department", "op": "=", "val": department},
                     {"col": "JobRole", "op": "=", "val": "Manager"}],
            group_by=[], metric="avg", target="MonthlyIncome")
        decision = gate.precheck(query, who.role, policy)
        if decision is None:
            cells = executor.run_query(db, query, now, session=session)
            # No prior releases or changed employees can exist after the guards.
            decision = gate.check(query, who.role, cells, [], {}, policy)
        if decision.status != "allowed" or not decision.cells:
            raise WarmupError(f"{department} warmup was not fully allowed "
                              f"({decision.rule or 'empty population'})")
        entry = {
            "kind": "gate_decision", "time": now, "user": who.user,
            "role": who.role, "team": who.team, "project": PROJECT,
            "session_id": SESSION_ID, "memory_on": True,
            "query": query.model_dump(), "policy_version": int(policy["version"]),
            **evidence.audit_view(decision),
        }
        prepared.append((who, query, decision, entry))

    return sum(releases.record_then_release(
        db, decision, query, who, PROJECT, SESSION_ID, now, policy, entry,
        session=session) for who, query, decision, entry in prepared)


def run_warmup(db, *, session=None) -> int:
    """Write both Monday releases atomically and return their count.

    An existing transaction may be supplied by reset; it must commit before the
    caller reports success. Standalone calls create and commit their own transaction.
    """
    with operation_lock:
        if session is not None:
            if not session.in_transaction:
                raise ValueError("warmup requires an active transaction")
            return _run(db, session)
        with db.client.start_session() as own_session:
            return own_session.with_transaction(lambda txn: _run(db, txn))
