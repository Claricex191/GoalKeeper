"""A transaction must preserve the executor's authorization and data checks."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.clock import DEMO_START
from app.policy.defaults import POLICY
from app.query import executor
from app.schemas import StructuredQuery
from app.users import DEMO_USERS


def test_transaction_reads_its_scoped_schema_and_aggregation(monkeypatch):
    # A stale process schema must not hide the integer type restored by reset.
    cached = {"MonthlyIncome": str}
    monkeypatch.setattr(executor, "_field_types_cache", cached)
    sample = {"EmployeeNumber": 1, "Department": "Sales", "MonthlyIncome": 100}
    row = {"_id": None, "ids": [1, 2, 3, 4, 5], "n": 5,
           "departments": ["Sales"], "values": [100, 200, 300, 400, 500]}
    employees = SimpleNamespace(find_one=Mock(return_value=sample),
                                aggregate=Mock(return_value=[row]))
    session = object()
    [cell] = executor.run_query(SimpleNamespace(employees=employees),
                               StructuredQuery(metric="avg", target="MonthlyIncome"),
                               DEMO_START, DEMO_USERS["mgr_1"], POLICY, session=session)
    assert cell.value == 300 and cell.departments == {"Sales"}
    expected_clauses = [{"active": True}, {"start_date": {"$lte": DEMO_START}},
                        {"Department": {"$in": ["Sales"]}}]
    assert employees.find_one.call_args.args[0] == {"$and": expected_clauses}
    assert employees.aggregate.call_args.args[0][0] == {"$match": {"$and": expected_clauses}}
    assert employees.find_one.call_args.kwargs == {"session": session}
    assert employees.aggregate.call_args.kwargs == {"session": session}
    assert executor._field_types_cache is cached


def test_transaction_does_not_bypass_authorization(monkeypatch):
    monkeypatch.setattr(executor, "_field_types_cache", None)
    employees = SimpleNamespace(find_one=Mock(), aggregate=Mock())
    with pytest.raises(executor.QueryError, match="not authorized"):
        executor.run_query(SimpleNamespace(employees=employees),
                           StructuredQuery(metric="avg", target="MonthlyIncome"),
                           DEMO_START, DEMO_USERS["emp_1"], POLICY, session=object())
    employees.find_one.assert_not_called()
    employees.aggregate.assert_not_called()


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), None, "500"])
def test_transaction_still_rejects_invalid_contributors(bad, monkeypatch):
    monkeypatch.setattr(executor, "_field_types_cache", None)
    row = {"_id": None, "ids": [1, 2, 3, 4, 5], "n": 5,
           "departments": ["Sales"], "values": [100, 200, 300, 400, bad]}
    employees = SimpleNamespace(find_one=Mock(return_value={"MonthlyIncome": 100}),
                                aggregate=Mock(return_value=[row]))
    with pytest.raises(executor.QueryError, match="present, numeric and finite"):
        executor.run_query(SimpleNamespace(employees=employees),
                           StructuredQuery(metric="median", target="MonthlyIncome"),
                           DEMO_START, DEMO_USERS["mgr_1"], POLICY, session=object())
