"""Offline regressions for the merged tool protocol, scoped metadata and L5.

The executor, Gate, release assembly and review functions run normally. Mongomock
has no transactions, so the session shim passes None to transaction callbacks;
these tests do not claim to verify MongoDB's atomic transaction semantics.
"""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.agent import tools
from app.clock import DEMO_START
from app.policy.defaults import POLICY
from app.query import executor
from app.review import approvals
from app.schemas import Identity
from app.users import DEMO_USERS


@pytest.fixture
def context(monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient(tz_aware=True)
    db = client["merged_tools"]
    rows = []
    for offset, department in ((1000, "Sales"), (2000, "Human Resources")):
        for i in range(12):
            rows.append({
                "EmployeeNumber": offset + i, "Department": department,
                "JobRole": "Manager" if i < 6 else "Other",
                "JobLevel": 3 if i < 6 else 2, "Gender": "Female" if i % 2 else "Male",
                "MonthlyIncome": 1000 + i * 100, "PerformanceRating": 3 + i % 2,
                "Age": 30 + i, "AgeBand": "30-34", "StockOptionLevel": 1,
                "active": True, "start_date": DEMO_START, "updated_at": {},
            })
    db.employees.insert_many(rows)

    @contextmanager
    def start_session():
        yield SimpleNamespace(with_transaction=lambda callback: callback(None))

    monkeypatch.setattr(client, "start_session", start_session)
    monkeypatch.setattr(executor, "_field_types_cache", None)
    ctx = {"db": db, "who": DEMO_USERS["mgr_1"], "project": "merge-test",
           "session_id": "s", "memory": True, "now": DEMO_START,
           # L5 mechanics are tested at limit 5, independent of the demo setting
           "policy": {**POLICY, "version": 2,
                      "probing": {"max_attempts": 5, "window_days": 7}}}
    yield ctx
    client.close()


def income(**changes):
    return {"filters": [{"col": "JobRole", "op": "=", "val": "Manager"}],
            "metric": "avg", "target": "MonthlyIncome", **changes}


def ask(context, turn, query=None, **changes):
    ctx = {**context, "turn_id": turn, **changes}
    return tools.DISPATCH["query_hr"](ctx, query or income())


@pytest.mark.parametrize("approve", [False, True], ids=["reject-review", "approve-review"])
def test_l5_turn_count_pending_and_review_reset_survive_the_permission_merge(context, approve):
    db = context["db"]
    blocked = ask(context, "t0", income(metric="max"))
    assert blocked["trace"]["rule"] == "L1.extreme"
    same_turn = ask(context, "t0", income(transform="T4_extreme_to_avg"))
    assert same_turn["for_llm"]["status"] == "allowed"
    for turn in range(1, 6):
        assert ask(context, f"t{turn}")["trace"]["status"] == "allowed"
    assert ask(context, "t5", income(filters=[], transform="T3_widen_scope"))["trace"]["status"] == "allowed"
    assert ask(context, "off", memory=False)["trace"]["status"] == "allowed"
    assert ask(context, "other-user", who=DEMO_USERS["mgr_2"])["trace"]["status"] == "allowed"

    before = db.releases.count_documents({})
    sixth = ask(context, "t6")
    assert sixth["trace"]["rule"] == "L5.probing"
    assert sixth["for_llm"]["review"] == "pending"
    assert sixth["for_llm"]["cells"] == []
    assert db.releases.count_documents({}) == before
    approval_id = sixth["trace"]["approval_id"]
    assert [row["id"] for row in approvals.list_pending(db, "merge-test")] == [approval_id]
    assert ask(context, "t7")["trace"]["rule"] == "L5.pending"
    assert ask(context, "other-field", {"metric": "avg", "target": "PerformanceRating"})["trace"]["status"] == "allowed"
    assert approvals.decide(db, approval_id, approve, None, "hr_1", DEMO_START)
    assert not approvals.decide(db, approval_id, not approve, None, "hr_1", DEMO_START)
    assert ask(context, "t8")["trace"]["status"] == "allowed"
    assert approvals.list_pending(db, "merge-test") == []


def test_authorization_precedes_probing_and_never_escalates_denied_fields(context, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("L5 must not run for an unauthorized field")

    monkeypatch.setattr(approvals, "pending_for", unexpected)
    out = ask(context, "t", {"metric": "avg", "target": "Age"})
    assert out["trace"]["rule"] == "L1.role"
    assert out["for_llm"]["cells"] == []
    assert context["db"].approvals.count_documents({}) == 0


def test_pending_review_stops_before_the_executor(context, monkeypatch):
    approvals.create(context["db"], "mgr_1", "sales", "merge-test", "MonthlyIncome",
                     income(), "review required", DEMO_START)

    def unexpected(*args, **kwargs):
        raise AssertionError("Pending review must not reach employee queries")

    monkeypatch.setattr(executor, "run_query", unexpected)
    out = ask(context, "pending")
    assert out["trace"]["rule"] == "L5.pending"
    assert out["for_llm"]["cells"] == []


def test_individual_grant_is_separate_from_a_statistics_review_lock(context, monkeypatch):
    db = context["db"]
    approvals.create(db, "mgr_1", "sales", "merge-test", "PerformanceRating",
                     {"metric": "avg", "target": "PerformanceRating"}, "review", DEMO_START)

    def unexpected(*args, **kwargs):
        raise AssertionError("Individual mode must not enter aggregate probing")

    monkeypatch.setattr(tools, "_probing", unexpected)
    out = tools.DISPATCH["query_hr_individual"](context, {
        "mode": "individual", "employee_numbers": [1000], "fields": ["PerformanceRating"]})
    wanted = [{"request_index": 0, "values": {"PerformanceRating": 3}}]
    assert out["for_llm"]["status"] == out["trace"]["status"] == "allowed"
    assert out["for_llm"]["records"] == out["trace"]["records"] == wanted
    assert out["for_llm"]["cells"] == []
    assert db.releases.count_documents({}) == 0
    assert db.ledger.find_one({"query.mode": "individual"}) is not None
    assert "employee_numbers" not in json.dumps(out["for_llm"])


@pytest.mark.parametrize("supplied_mode", [None, "aggre_stats"])
def test_individual_dispatcher_fixes_mode_to_match_the_tool_name(context, supplied_mode):
    args = {"employee_numbers": [1000], "fields": ["PerformanceRating"]}
    if supplied_mode is not None:
        args["mode"] = supplied_mode
    out = tools.DISPATCH["query_hr_individual"](context, args)
    assert out["for_llm"]["status"] == "allowed"
    assert out["for_llm"]["records"] == [
        {"request_index": 0, "values": {"PerformanceRating": 3}}]
    assert out["trace"]["tool"] == "query_hr_individual"
    assert out["trace"]["query"]["mode"] == "individual"
    assert context["db"].ledger.find_one({"query.mode": "individual", "status": "allowed"})
    assert context["db"].releases.count_documents({}) == 0


def test_individual_dispatcher_rejects_aggregate_arguments(context):
    out = tools.DISPATCH["query_hr_individual"](context, {
        "mode": "aggre_stats", "metric": "avg", "target": "MonthlyIncome"})
    assert out["for_llm"]["status"] == "error"
    assert out["trace"]["tool"] == "query_hr_individual"
    assert out["for_llm"]["cells"] == [] and not out["for_llm"].get("records")
    assert context["db"].ledger.find_one({"query.mode": "individual", "rule": "invalid"})
    assert context["db"].releases.count_documents({}) == 0


def test_invalid_individual_selectors_return_a_validation_error(context):
    out = tools.DISPATCH["query_hr_individual"](context, {
        "employee_numbers": None, "fields": ["PerformanceRating"]})
    assert out["for_llm"]["status"] == out["trace"]["status"] == "error"
    assert out["trace"]["tool"] == "query_hr_individual"
    assert out["trace"]["query"]["requested_count"] == 0
    assert out["for_llm"]["cells"] == [] and not out["for_llm"].get("records")
    assert context["db"].ledger.find_one({"query.mode": "individual", "rule": "invalid"})
    assert context["db"].releases.count_documents({}) == 0


def test_client_turn_ids_cannot_bypass_trusted_message_counting(context):
    out = ask(context, "server-turn", income(turn_id="forged"))
    assert out["for_llm"]["status"] in ("blocked", "error")
    assert out["for_llm"]["cells"] == []
    assert context["db"].releases.count_documents({}) == 0


class NoEmployeeReads:
    @property
    def employees(self):
        raise AssertionError("Tool metadata must not enumerate employee data")


def test_tool_metadata_uses_scoped_policy_catalogs_without_employee_reads():
    db = NoEmployeeReads()
    sales = tools.tool_specs(POLICY, DEMO_USERS["mgr_1"], db=db, now=DEMO_START)
    rnd = tools.tool_specs(POLICY, DEMO_USERS["mgr_3"], db=db, now=DEMO_START)
    assert "Sales" in json.dumps(sales)
    assert "Research & Development" not in json.dumps(sales)
    assert "Research & Development" in json.dumps(rnd)
    assert "AgeBand" not in json.dumps(sales)
    assert "StockOptionLevel" not in json.dumps(sales)
    # Same memory team cannot create a cache-based metadata scope bypass.
    reassigned = Identity("another", "department_manager", "sales", "R&D", ("Research & Development",))
    assert "Research & Development" in json.dumps(tools.tool_specs(POLICY, reassigned, db=db))
    assert "Research & Development" not in json.dumps(tools.tool_specs(POLICY, DEMO_USERS["mgr_1"], db=db))


def test_legacy_role_metadata_has_no_employee_or_assigned_department_hints():
    specs = tools.tool_specs(POLICY, "department_manager", db=NoEmployeeReads())
    assert specs
    text = json.dumps(specs)
    for department in POLICY["departments"]:
        assert department not in text


def test_no_tools_are_exposed_to_an_employee_or_an_unknown_role():
    assert tools.tool_specs(POLICY, DEMO_USERS["emp_1"], db=NoEmployeeReads()) == []
    assert tools.tool_specs(POLICY, "unknown", db=NoEmployeeReads()) == []


def test_policy_l5_limit_is_three():
    assert POLICY["probing"] == {"max_attempts": 3, "window_days": 7}
