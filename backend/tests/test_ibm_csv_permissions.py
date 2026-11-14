"""Full IBM CSV → actual loader → executor → Gate, without a remote database.

Mongomock executes the application's Mongo aggregation pipeline; it is not Atlas.
Expected populations and statistics are calculated independently from the original
1,470 CSV rows using csv/statistics, not from the policy or executor implementation.
"""
import csv
from itertools import product
from pathlib import Path
from statistics import mean, median

import pytest

from app import gate
from app.clock import DEMO_START, NEW_HIRE_START
from app.gate.evidence import public_view
from app.policy.defaults import POLICY
from app.query import executor
from app.query.executor import QueryError, run_individual_query, run_query
from app.schemas import Identity, IndividualQuery, Release, StructuredQuery

CSV = Path(__file__).resolve().parents[1] / "data" / "ibm_hr.csv"

# Product specification, in Other Basic / Sensitive Basic / Salary /
# Measurement Report / Performance Rating order. I and A remain independent.
GRANTS = {
    "ceo":                   ("a", "",   "a",  "a",  "a"),
    "vp":                    ("a", "",   "a",  "a",  "a"),
    "hr_head":               ("ia", "ia", "ia", "ia", "ia"),
    "hr_salary_manager":     ("ia", "ia", "ia", "a",  "a"),
    "department_hr_manager": ("a",  "a",  "a",  "a",  "a"),
    "department_manager":    ("a", "",   "a",  "a",  "ia"),
    "ordinary_hr":           ("i", "i",  "",   "",   "a"),
    "employee":              ("",  "",   "",   "",   ""),
}
FIELDS = ("YearsAtCompany", "Age", "MonthlyIncome", "JobSatisfaction", "PerformanceRating")
COMPANY_ROLES = {"ceo", "hr_head", "hr_salary_manager"}


def identity(role):
    department = "Human Resources" if role == "ordinary_hr" else "Sales"
    return Identity(f"csv_{role}", role, "csv_validation", role, (department,))


@pytest.fixture(scope="module")
def rows():
    if not CSV.exists():
        pytest.skip("Full IBM CSV required: backend/data/ibm_hr.csv")

    def scalar(text):
        try:
            return int(text)
        except ValueError:
            return text

    with CSV.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert len(reader.fieldnames) == 35
        data = [{key: scalar(value) for key, value in row.items()} for row in reader]
    assert len(data) == 1470
    assert len({row["EmployeeNumber"] for row in data}) == 1470
    assert {department: sum(r["Department"] == department for r in data)
            for department in {r["Department"] for r in data}} == {
        "Sales": 446, "Research & Development": 961, "Human Resources": 63}
    return data


@pytest.fixture(scope="module")
def db(rows):
    mongomock = pytest.importorskip("mongomock", reason="CSV executor tests use local Mongo emulation")
    pd = pytest.importorskip("pandas", reason="The application's CSV loader requires pandas")
    from scripts.load_data import build_documents, check

    frame = pd.read_csv(CSV)
    check(frame)
    documents = build_documents(frame)
    assert len(documents) == len(rows)
    client = mongomock.MongoClient(tz_aware=True)
    database = client["ibm_csv_permissions"]
    database.employees.insert_many(documents)
    yield database
    client.close()


@pytest.fixture(autouse=True)
def reset_schema_sample(monkeypatch):
    # Exercise the real scoped first-read path rather than a mock type cache.
    monkeypatch.setattr(executor, "_field_types_cache", None)


def population(rows, role):
    if role in COMPANY_ROLES:
        return rows
    department = "Human Resources" if role == "ordinary_hr" else "Sales"
    return [row for row in rows if row["Department"] == department]


@pytest.mark.parametrize("role, category, mode", list(product(GRANTS, range(5), ("i", "a"))))
def test_full_csv_role_category_operation_matrix(db, rows, role, category, mode):
    who, field = identity(role), FIELDS[category]
    expected_rows = population(rows, role)
    employee = expected_rows[0]
    allowed = mode in GRANTS[role][category]
    if mode == "i":
        query = IndividualQuery(mode="individual", employee_numbers=[employee["EmployeeNumber"]],
                                fields=[field])
        run = lambda: run_individual_query(db, query, NEW_HIRE_START, who, POLICY)
    else:
        query = StructuredQuery(metric="avg", target=field)
        run = lambda: run_query(db, query, NEW_HIRE_START, who, POLICY)
    decision = gate.precheck(query, who, POLICY)
    if not allowed:
        assert decision is not None and decision.status == "blocked"
        with pytest.raises(QueryError, match="not authorized"):
            run()
        return
    assert decision is None
    result = run()
    if mode == "i":
        assert len(result) == 1
        assert result[0].employee_number == employee["EmployeeNumber"]
        assert result[0].values == {field: employee[field]}
        approved = gate.check_individual(query, who, result, POLICY)
        assert approved.status == "allowed"
        assert public_view(approved)["records"] == [
            {"request_index": 0, "values": {field: employee[field]}}]
    else:
        assert len(result) == 1
        actual = result[0]
        assert actual.ids == {row["EmployeeNumber"] for row in expected_rows}
        assert actual.n == len(expected_rows)
        assert actual.value == pytest.approx(mean(row[field] for row in expected_rows))
        assert actual.departments == {row["Department"] for row in expected_rows}
        assert gate.check(query, who, result, [], {}, POLICY).status == "allowed"


@pytest.mark.parametrize("department", [None, "Sales", "Research & Development", "Human Resources"])
@pytest.mark.parametrize("metric", ["avg", "median", "sum"])
def test_full_csv_numeric_statistics_match_independent_reference(db, rows, department, metric):
    filters = [] if department is None else [{"col": "Department", "op": "=", "val": department}]
    query = StructuredQuery(metric=metric, target="MonthlyIncome", filters=filters)
    expected_rows = rows if department is None else [r for r in rows if r["Department"] == department]
    values = [row["MonthlyIncome"] for row in expected_rows]
    who = identity("hr_head")
    cells = run_query(db, query, NEW_HIRE_START, who, POLICY)
    assert len(cells) == 1
    assert cells[0].n == len(expected_rows)
    assert cells[0].value == pytest.approx({"avg": mean, "median": median, "sum": sum}[metric](values))
    assert gate.check(query, who, cells, [], {}, POLICY).status == "allowed"


@pytest.mark.parametrize("role", ["vp", "department_hr_manager", "department_manager", "ordinary_hr"])
def test_full_csv_omitted_department_filter_still_has_server_scope(db, rows, role):
    who = identity(role)
    query = StructuredQuery(metric="avg", target="PerformanceRating")
    cells = run_query(db, query, NEW_HIRE_START, who, POLICY)
    assert cells[0].ids == {r["EmployeeNumber"] for r in population(rows, role)}
    assert cells[0].n == (63 if role == "ordinary_hr" else 446)


@pytest.mark.parametrize("transform", ["T2_drop_filter", "T3_widen_scope"])
def test_full_csv_rewrites_cannot_leave_sales_department(db, rows, transform):
    query = StructuredQuery(metric="avg", target="MonthlyIncome", transform=transform)
    who = identity("department_manager")
    cells = run_query(db, query, NEW_HIRE_START, who, POLICY)
    assert cells[0].ids == {r["EmployeeNumber"] for r in rows if r["Department"] == "Sales"}
    assert cells[0].n == 446


@pytest.mark.parametrize("role", ["department_manager", "ordinary_hr"])
def test_full_csv_individual_lookup_omits_people_outside_scope(db, rows, role):
    who = identity(role)
    own_department = who.departments[0]
    own = next(r for r in rows if r["Department"] == own_department)
    other = next(r for r in rows if r["Department"] != own_department)
    field = "Age" if role == "ordinary_hr" else "PerformanceRating"
    query = IndividualQuery(mode="individual", employee_numbers=[other["EmployeeNumber"], own["EmployeeNumber"]],
                            fields=[field])
    records = run_individual_query(db, query, NEW_HIRE_START, who, POLICY)
    assert [r.employee_number for r in records] == [own["EmployeeNumber"]]
    decision = gate.check_individual(query, who, records, POLICY)
    assert public_view(decision)["records"] == [
        {"request_index": 1, "values": {field: own[field]}}]


@pytest.mark.parametrize("field", FIELDS)
def test_full_csv_department_hr_manager_cannot_lookup_individuals_in_own_department(db, rows, field):
    who = identity("department_hr_manager")
    employee = population(rows, who.role)[0]
    query = IndividualQuery(mode="individual", employee_numbers=[employee["EmployeeNumber"]],
                            fields=[field])
    decision = gate.precheck(query, who, POLICY)
    assert decision.status == "blocked" and decision.rule == "L1.role"
    assert not public_view(decision).get("records")
    with pytest.raises(QueryError, match="not authorized"):
        run_individual_query(db, query, NEW_HIRE_START, who, POLICY)


def test_full_csv_sensitive_individual_fields_are_correct_and_projected(db, rows):
    employee = rows[0]
    fields = ["Age", "DistanceFromHome", "MaritalStatus", "Over18", "StockOptionLevel"]
    query = IndividualQuery(mode="individual", employee_numbers=[employee["EmployeeNumber"]], fields=fields)
    who = identity("hr_head")
    records = run_individual_query(db, query, NEW_HIRE_START, who, POLICY)
    decision = gate.check_individual(query, who, records, POLICY)
    view = public_view(decision)
    assert view["records"] == [{"request_index": 0, "values": {f: employee[f] for f in fields}}]
    assert "EmployeeNumber" not in str(view) and "MonthlyIncome" not in str(view)


@pytest.mark.parametrize("role", ["ceo", "vp", "department_manager"])
def test_full_csv_salary_grants_do_not_include_stock_option_level(db, role):
    who = identity(role)
    salary = StructuredQuery(metric="avg", target="MonthlyIncome")
    assert run_query(db, salary, NEW_HIRE_START, who, POLICY)
    stock_options = StructuredQuery(metric="avg", target="StockOptionLevel")
    with pytest.raises(QueryError, match="not authorized"):
        run_query(db, stock_options, NEW_HIRE_START, who, POLICY)


def test_full_csv_four_hr_employees_mean_is_never_released(db, rows):
    query = StructuredQuery(metric="avg", target="MonthlyIncome", filters=[
        {"col": "Department", "op": "=", "val": "Human Resources"},
        {"col": "JobLevel", "op": "=", "val": 4},
    ])
    expected = [r for r in rows if r["Department"] == "Human Resources" and r["JobLevel"] == 4]
    assert len(expected) == 4
    who = identity("hr_head")
    cells = run_query(db, query, NEW_HIRE_START, who, POLICY)
    assert cells[0].n == 4
    assert cells[0].value == pytest.approx(mean(r["MonthlyIncome"] for r in expected))
    decision = gate.check(query, who, cells, [], {}, {**POLICY, "k_threshold": 2})
    assert decision.status == "blocked" and decision.rule == "L2.min_n"
    assert public_view(decision)["cells"] == []


def test_full_csv_homogeneous_performance_is_suppressed(db):
    query = StructuredQuery(metric="avg", target="PerformanceRating", filters=[
        {"col": "JobRole", "op": "=", "val": "Sales Executive"},
        {"col": "JobLevel", "op": "=", "val": 4},
    ])
    who = identity("hr_head")
    cells = run_query(db, query, NEW_HIRE_START, who, POLICY)
    assert len(cells) == 1 and cells[0].n == 14
    assert cells[0].value == cells[0].vmin == cells[0].vmax == 3
    decision = gate.check(query, who, cells, [], {}, POLICY)
    assert decision.status == "blocked" and decision.rule == "L2.homogeneous"
    assert public_view(decision)["cells"] == []


def test_full_csv_new_hire_demo_values_and_cross_query_block(db):
    query = StructuredQuery(metric="avg", target="MonthlyIncome", filters=[
        {"col": "JobRole", "op": "=", "val": "Manager"},
    ])
    who = identity("department_manager")
    before = run_query(db, query, DEMO_START, who, POLICY)
    after = run_query(db, query, NEW_HIRE_START, who, POLICY)
    assert (before[0].n, round(before[0].value, 2)) == (36, 17111.50)
    assert (after[0].n, round(after[0].value, 2)) == (37, 16986.97)
    assert after[0].ids - before[0].ids == {473}
    assert gate.check(query, who, before, [], {}, POLICY).status == "allowed"
    history = [Release(before[0].ids, "MonthlyIncome", "avg", DEMO_START, {}, "Before hire", who.user)]
    decision = gate.check(query, who, after, history, {}, POLICY)
    assert decision.status == "blocked" and decision.rule == "L4.pairwise"
    assert public_view(decision)["cells"] == []
