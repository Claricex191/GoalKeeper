"""Offline integration tests for the authorization/execution/release boundary."""
from types import SimpleNamespace

import pytest

from app.agent import tools
from app.clock import DEMO_START
from app.policy.defaults import POLICY
from app.query import executor
from app.schemas import Release
from app.users import DEMO_USERS


class Employees:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def aggregate(self, pipeline):
        self.calls.append(("aggregate", pipeline))
        return [{"_id": None, "ids": list(range(len(self.values))), "n": len(self.values),
                 "departments": ["Sales"], "values": self.values}]

    def find(self, query, projection):
        self.calls.append(("find", query, projection))
        if "updated_at" in projection:
            return [{"EmployeeNumber": i, "updated_at": {}} for i in range(len(self.values))]
        return [{"EmployeeNumber": 7, "Department": "Sales", "PerformanceRating": 4}]


@pytest.fixture
def context(monkeypatch):
    employees = Employees([1000, 2000, 3000, 4000, 5000, 6000])
    events = []
    ctx = {"db": SimpleNamespace(employees=employees), "who": DEMO_USERS["mgr_1"],
           "policy": {**POLICY, "version": 2}, "now": DEMO_START,
           "project": "offline", "session_id": "s", "memory": True}
    monkeypatch.setattr(executor, "_field_types_cache", {"MonthlyIncome": int})
    monkeypatch.setattr(tools.releases, "load_history", lambda *a: [])
    monkeypatch.setattr(tools.ledger, "append", lambda db, entry: events.append(("audit", entry)))
    monkeypatch.setattr(tools.releases, "record_then_release",
                        lambda *a: events.append(("release", a[1])))
    original = tools.evidence.public_view

    def public(*args, **kwargs):
        events.append(("public", None))
        return original(*args, **kwargs)

    monkeypatch.setattr(tools.evidence, "public_view", public)
    return ctx, employees, events


def test_successful_statistics_are_scoped_and_written_before_agent_receives_them(context):
    ctx, employees, events = context
    result = tools.handle_query_hr(ctx, {"metric": "avg", "target": "MonthlyIncome"})
    assert result["status"] == "allowed"
    assert result["cells"] == [{"key": {}, "n": 6, "value": 3500}]
    assert {"Department": {"$in": ["Sales"]}} in employees.calls[0][1][0]["$match"]["$and"]
    assert [kind for kind, _ in events] == ["release", "public"]
    assert "ids" not in str(result) and "departments" not in str(result)


@pytest.mark.parametrize("memory", [True, False])
def test_four_person_mean_never_reaches_agent_even_without_memory(context, memory):
    ctx, employees, events = context
    ctx["memory"] = memory
    ctx["policy"]["k_threshold"] = 2
    employees.values = [1000, 2000, 3000, 4000]
    result = tools.handle_query_hr(ctx, {"metric": "avg", "target": "MonthlyIncome"})
    assert result["status"] == "blocked" and result["cells"] == []
    assert [kind for kind, _ in events] == ["audit", "public"]
    assert "2500" not in str(result)


def test_employee_count_rejected_before_any_employee_read(context):
    ctx, employees, events = context
    ctx["who"] = DEMO_USERS["emp_1"]
    result = tools.handle_query_hr(ctx, {"metric": "count"})
    assert result["rule"] == "L1.role" and result["cells"] == []
    assert employees.calls == []
    assert events[0][0] == "audit"


@pytest.mark.parametrize("args", [
    {"metric": "avg", "target": "MonthlyIncome", "role": "hr_head"},
    {"mode": "individual", "fields": ["MonthlyIncome"], "employee_numbers": [7], "scope": "company"},
])
def test_tool_arguments_cannot_forge_identity_or_scope(context, args):
    ctx, employees, _ = context
    assert tools.handle_query_hr(ctx, args)["status"] == "blocked"
    assert employees.calls == []


def test_individual_performance_is_audited_without_a_one_person_aggregate_release(context):
    ctx, employees, events = context
    args = {"mode": "individual", "fields": ["PerformanceRating"], "employee_numbers": [7]}
    result = tools.handle_query_hr(ctx, args)
    assert result["status"] == "allowed"
    assert result["records"] == [{"request_index": 0, "values": {"PerformanceRating": 4}}]
    assert result["cells"] == []
    assert [kind for kind, _ in events] == ["audit", "public"]
    assert events[0][1]["query"]["mode"] == "individual"
    assert "values" not in events[0][1] and "records" not in events[0][1]
    assert "EmployeeNumber" not in str(result)
    assert {"Department": {"$in": ["Sales"]}} in employees.calls[0][1]["$and"]


@pytest.mark.parametrize("individual", [True, False])
def test_failed_audit_or_release_write_returns_no_data(context, monkeypatch, individual):
    ctx, _, _ = context

    def failure(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    if individual:
        monkeypatch.setattr(tools.ledger, "append", failure)
        args = {"mode": "individual", "employee_numbers": [7], "fields": ["PerformanceRating"]}
    else:
        monkeypatch.setattr(tools.releases, "record_then_release", failure)
        args = {"metric": "avg", "target": "MonthlyIncome"}
    result = tools.handle_query_hr(ctx, args)
    assert result["status"] == "blocked" and result["rule"] == "gate.error"
    assert result["cells"] == [] and not result.get("records")


def test_existing_median_prevents_linear_release_through_tool(context, monkeypatch):
    ctx, _, events = context
    old = Release(set(range(6)), "MonthlyIncome", "median", DEMO_START, {}, "Earlier median")
    monkeypatch.setattr(tools.releases, "load_history", lambda *a: [old])
    result = tools.handle_query_hr(ctx, {"metric": "avg", "target": "MonthlyIncome"})
    assert result["status"] == "blocked" and result["cells"] == []
    assert events[0][0] == "audit"


def test_first_safe_median_can_be_released_through_tool(context):
    ctx, employees, events = context
    employees.values = list(range(10))
    result = tools.handle_query_hr(ctx, {"metric": "median", "target": "MonthlyIncome"})
    assert result["status"] == "allowed"
    assert result["cells"] == [{"key": {}, "n": 10, "value": 4.5}]
    assert events[0][0] == "release"
