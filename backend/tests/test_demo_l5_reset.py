"""A fresh demo must not inherit probing counts from its retained audit ledger.

Uses the main tool regression fixture's local Mongo adapter; transaction rollback
is covered separately by test_demo_reset, not claimed by these integration tests.
"""

from copy import deepcopy

from app.clock import DEMO_START
from app.demo import reset
from app.policy.defaults import POLICY
from app.review import approvals
from test_tools_merge import ask, context, income


def trigger_review(ctx, prefix):
    assert ask(ctx, f"{prefix}-first", income(metric="max"))["trace"]["rule"] == "L1.extreme"
    for index in range(5):
        assert ask(ctx, f"{prefix}-{index}")["trace"]["status"] == "allowed"
    result = ask(ctx, f"{prefix}-sixth")
    assert result["trace"]["rule"] == "L5.probing"
    return result["trace"]["approval_id"]


def test_reset_keeps_audit_but_starts_new_probing_interval(context, monkeypatch):
    db = context["db"]
    originals = list(db.employees.find({}, {"_id": 0}))
    monkeypatch.setattr(reset, "load_baseline", lambda: deepcopy(originals))
    db.policies.insert_one({**deepcopy(POLICY), "dataset": "ibm_hr", "version": 2,
                            "status": "active"})

    trigger_review(context, "old")
    retained = list(db.ledger.find({}).sort("_id", 1))
    old_last = retained[-1]["_id"]
    assert approvals.pending_for(db, "mgr_1", context["project"], "MonthlyIncome")

    assert reset.reset_demo(db) == DEMO_START
    assert db.meta.find_one({"_id": "demo_reset"})["ledger_after"] == old_last
    assert list(db.ledger.find({"_id": {"$lte": old_last}}).sort("_id", 1)) == retained
    assert db.approvals.count_documents({"kind": "review"}) == 0
    assert db.releases.count_documents({"session_id": "warmup"}) == 2
    assert ask(context, "new-safe")["trace"]["status"] == "allowed"

    # Main's L5 protection is still active in this run, and a subsequent reviewer
    # decision takes precedence over the earlier demo reset boundary.
    approval_id = trigger_review(context, "new")
    assert approvals.decide(db, approval_id, True, None, "hr_1", DEMO_START)
    assert ask(context, "reviewed")["trace"]["status"] == "allowed"
