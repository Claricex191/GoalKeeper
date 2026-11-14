"""Execution boundary regressions, entirely offline with in-memory database spies."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.clock import DEMO_START
from app.policy.defaults import POLICY
from app.query import executor
from app.query.executor import QueryError, build_pipeline, run_individual_query, run_query
from app.schemas import Identity, IndividualQuery, StructuredQuery

HR_HEAD = Identity("head", "hr_head", "hr", "HR Head")
MANAGER = Identity("manager", "department_manager", "sales", "Sales manager", ("Sales",))
ORDINARY_HR = Identity("hr", "ordinary_hr", "hr", "HR staff", ("Human Resources",))
EMPLOYEE = Identity("employee", "employee", "sales", "Employee", ("Sales",))
TYPES = {"MonthlyIncome": int, "PerformanceRating": int, "Department": str,
         "Age": int, "Gender": str, "EmployeeNumber": int}


class Employees:
    def __init__(self, aggregate_rows=(), individual_rows=()):
        self.aggregate_rows = list(aggregate_rows)
        self.individual_rows = list(individual_rows)
        self.calls = []

    def aggregate(self, pipeline):
        self.calls.append(("aggregate", pipeline))
        return iter(self.aggregate_rows)

    def find(self, query, projection):
        self.calls.append(("find", query, projection))
        return iter(self.individual_rows)

    def find_one(self, *args, **kwargs):
        self.calls.append(("find_one",))
        raise AssertionError("Unexpected schema sample read")


@pytest.fixture(autouse=True)
def field_types(monkeypatch):
    monkeypatch.setattr(executor, "_field_types_cache", TYPES)


def salary_query(**kwargs):
    return StructuredQuery(metric="avg", target="MonthlyIncome", **kwargs)


def row(values, departments=None):
    return {"_id": None, "ids": list(range(1, len(values) + 1)), "n": len(values),
            "departments": departments or ["Sales"], "values": values}


@pytest.mark.parametrize("query", [
    salary_query(),
    salary_query(transform="T2_drop_filter"),
    salary_query(transform="T3_widen_scope"),
    salary_query(filters=[{"col": "Department", "op": "!=", "val": "Human Resources"}]),
])
def test_rewrites_cannot_remove_server_department_scope(query):
    pipeline = build_pipeline(query, DEMO_START, TYPES, MANAGER, POLICY)
    assert {"Department": {"$in": ["Sales"]}} in pipeline[0]["$match"]["$and"]
    assert pipeline[1]["$group"]["departments"] == {"$addToSet": "$Department"}


def test_company_scope_does_not_inherit_hr_team_boundary():
    pipeline = build_pipeline(salary_query(), DEMO_START, TYPES, HR_HEAD, POLICY)
    assert pipeline[0]["$match"]["$and"] == [
        {"active": True}, {"start_date": {"$lte": DEMO_START}}]


@pytest.mark.parametrize("who", [EMPLOYEE,
    Identity("missing", "department_manager", "sales", "Missing assignment"), "hr_head"])
def test_unauthorized_statistical_queries_never_touch_database(who, monkeypatch):
    monkeypatch.setattr(executor, "_field_types_cache", None)
    collection = Employees()
    with pytest.raises(QueryError):
        run_query(SimpleNamespace(employees=collection), salary_query(), DEMO_START, who, POLICY)
    assert collection.calls == []


def test_client_cannot_supply_scope_or_identity():
    with pytest.raises(ValidationError):
        StructuredQuery(metric="count", departments=["Sales"], role="hr_head")


def test_outside_department_filter_cannot_broaden_scope():
    # A conflicting filter may be denied immediately or intersected with the scope.
    query = salary_query(filters=[{"col": "Department", "op": "=", "val": "Human Resources"}])
    try:
        pipeline = build_pipeline(query, DEMO_START, TYPES, MANAGER, POLICY)
    except QueryError:
        return
    assert {"Department": {"$in": ["Sales"]}} in pipeline[0]["$match"]["$and"]
    assert {"Department": {"$eq": "Human Resources"}} in pipeline[0]["$match"]["$and"]


@pytest.mark.parametrize("values, expected", [([1, 8, 3, 5, 9], 5), ([1, 8, 3, 5, 9, 10], 6.5)])
def test_exact_median_does_not_return_raw_values(values, expected):
    collection = Employees([row(values)])
    query = StructuredQuery(metric="median", target="MonthlyIncome")
    [cell] = run_query(SimpleNamespace(employees=collection), query, DEMO_START, MANAGER, POLICY)
    assert cell.value == expected
    assert cell.n == len(values)
    assert cell.departments == {"Sales"}
    assert not hasattr(cell, "values")


@pytest.mark.parametrize("metric", ["avg", "sum", "median"])
@pytest.mark.parametrize("bad", [None, "100", float("nan"), float("inf"), True])
def test_invalid_contributors_are_not_counted_as_valid_observations(metric, bad):
    collection = Employees([row([1, 2, 3, 4, bad])])
    query = StructuredQuery(metric=metric, target="MonthlyIncome")
    with pytest.raises(QueryError, match="present, numeric and finite"):
        run_query(SimpleNamespace(employees=collection), query, DEMO_START, HR_HEAD, POLICY)


def test_missing_target_cannot_inflate_mean_population_count():
    result = row([1, 2, 3, 4])
    result.update(n=5, ids=[1, 2, 3, 4, 5])
    collection = Employees([result])
    with pytest.raises(QueryError, match="present, numeric and finite"):
        run_query(SimpleNamespace(employees=collection), salary_query(), DEMO_START, HR_HEAD, POLICY)


def test_bad_database_scope_evidence_is_rejected():
    collection = Employees([row([1, 2, 3, 4, 5], ["Human Resources"])])
    with pytest.raises(QueryError, match="outside the authorized scope"):
        run_query(SimpleNamespace(employees=collection), salary_query(), DEMO_START, MANAGER, POLICY)


def test_individual_query_has_mandatory_scope_and_exact_projection():
    collection = Employees(individual_rows=[{
        "EmployeeNumber": 7, "Department": "Human Resources", "Age": 31,
        # Even a database adapter accidentally returning extras cannot expose them.
        "MonthlyIncome": 9000,
    }])
    query = IndividualQuery(mode="individual", employee_numbers=[7], fields=["Age"])
    [record] = run_individual_query(SimpleNamespace(employees=collection), query,
                                    DEMO_START, ORDINARY_HR, POLICY)
    _, filt, projection = collection.calls[0]
    assert {"Department": {"$in": ["Human Resources"]}} in filt["$and"]
    assert {"EmployeeNumber": {"$in": [7]}} in filt["$and"]
    assert projection == {"Age": 1, "EmployeeNumber": 1, "Department": 1, "_id": 0}
    assert record.values == {"Age": 31}


def test_individual_outside_scope_is_never_returned():
    collection = Employees(individual_rows=[{"EmployeeNumber": 7, "Department": "Sales", "Age": 31}])
    query = IndividualQuery(mode="individual", employee_numbers=[7], fields=["Age"])
    with pytest.raises(QueryError, match="outside the authorized scope"):
        run_individual_query(SimpleNamespace(employees=collection), query,
                             DEMO_START, ORDINARY_HR, POLICY)


@pytest.mark.parametrize("who, fields", [(ORDINARY_HR, ["MonthlyIncome"]),
                                          (MANAGER, ["Age"]), (EMPLOYEE, ["Gender"])])
def test_unauthorized_individual_fields_never_touch_database(who, fields):
    collection = Employees()
    query = IndividualQuery(mode="individual", employee_numbers=[7], fields=fields)
    with pytest.raises(QueryError, match="not authorized"):
        run_individual_query(SimpleNamespace(employees=collection), query, DEMO_START, who, POLICY)
    assert collection.calls == []


def test_department_manager_can_read_individual_performance():
    collection = Employees(individual_rows=[{"EmployeeNumber": 7, "Department": "Sales", "PerformanceRating": 4}])
    query = IndividualQuery(mode="individual", employee_numbers=[7], fields=["PerformanceRating"])
    [record] = run_individual_query(SimpleNamespace(employees=collection), query, DEMO_START, MANAGER, POLICY)
    assert record.values == {"PerformanceRating": 4}


def test_initial_schema_sample_is_restricted_to_authorized_population(monkeypatch):
    monkeypatch.setattr(executor, "_field_types_cache", None)

    class FirstReadEmployees(Employees):
        def find_one(self, query, projection):
            self.calls.append(("find_one", query, projection))
            return {"EmployeeNumber": 7, "Department": "Sales", "MonthlyIncome": 5000}

    collection = FirstReadEmployees()
    assert run_query(SimpleNamespace(employees=collection), salary_query(),
                     DEMO_START, MANAGER, POLICY) == []
    operation, query, _ = collection.calls[0]
    assert operation == "find_one"
    assert query == {"$and": [
        {"active": True}, {"start_date": {"$lte": DEMO_START}},
        {"Department": {"$in": ["Sales"]}},
    ]}
