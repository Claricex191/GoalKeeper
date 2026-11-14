"""Role matrix and scope regressions, independent of MongoDB and the LLM."""

from copy import deepcopy
from datetime import datetime, timezone
from itertools import product

import pytest
from pydantic import ValidationError

from app.gate import check, check_individual, evidence, precheck
from app.policy.defaults import POLICY
from app.schemas import Cell, Identity, IndividualQuery, IndividualRecord, Release, StructuredQuery


# Expected grants transcribed from the product specification, not read from POLICY.
# Category order: Other Basic, Sensitive Basic, Salary, Measurement, Performance.
EXPECTED = {
    "ceo":                   ("a", "",  "a",  "a",  "a"),
    "vp":                    ("a", "",  "a",  "a",  "a"),
    "hr_head":               ("ia", "ia", "ia", "ia", "ia"),
    "hr_salary_manager":     ("ia", "ia", "ia", "a",  "a"),
    "department_hr_manager": ("a",  "a",  "a",  "a",  "a"),
    "department_manager":    ("a", "",  "a",  "a",  "ia"),
    "ordinary_hr":           ("i", "i", "",   "",   "a"),
    "employee":              ("",  "",  "",   "",   ""),
}
FIELDS = ("YearsAtCompany", "Age", "MonthlyIncome", "JobSatisfaction", "PerformanceRating")
MODES = {"i": "individual", "a": "aggre_stats"}


def identity(role, departments=None):
    if departments is None:
        departments = ("Human Resources",) if role == "ordinary_hr" else ("Sales",)
    return Identity(f"test_{role}", role, "test_team", role, tuple(departments))


def aggregate(field="MonthlyIncome", **kwargs):
    return StructuredQuery(metric="avg", target=field, **kwargs)


def individual(*fields):
    return IndividualQuery(mode="individual", employee_numbers=[7], fields=list(fields))


def cell(n=6, departments=None, **kwargs):
    return Cell(key={}, ids=set(range(n)), n=n, value=1.5, vmin=1, vmax=2,
                departments=departments, **kwargs)


@pytest.mark.parametrize("role, index, mode", list(product(EXPECTED, range(5), MODES)),
                         ids=lambda value: str(value))
def test_role_category_operation_matrix(role, index, mode):
    field = FIELDS[index]
    query = individual(field) if mode == "i" else aggregate(field)
    decision = precheck(query, identity(role), POLICY)
    assert (decision is None) == (mode in EXPECTED[role][index])
    if decision is not None:
        assert decision.status == "blocked"
        assert decision.allowed_transforms == []


@pytest.mark.parametrize("role", ["ceo", "vp", "department_manager", "employee"])
@pytest.mark.parametrize("field", ["Age", "DistanceFromHome", "MaritalStatus", "Over18",
                                    "StockOptionLevel", "AgeBand"])
def test_non_hr_cannot_access_sensitive_basic_individual_fields(role, field):
    decision = precheck(individual(field), identity(role), POLICY)
    assert decision is not None and decision.status == "blocked"


@pytest.mark.parametrize("field", ["Age", "DistanceFromHome", "MaritalStatus", "Over18",
                                    "StockOptionLevel", "AgeBand"])
def test_hr_head_can_read_every_sensitive_basic_field(field):
    assert precheck(individual(field), identity("hr_head"), POLICY) is None


@pytest.mark.parametrize("role", ["ceo", "vp", "department_manager"])
def test_salary_statistics_do_not_grant_stock_option_statistics(role):
    who = identity(role)
    assert precheck(aggregate(), who, POLICY) is None
    decision = precheck(aggregate("StockOptionLevel"), who, POLICY)
    assert decision is not None and decision.status == "blocked"


def test_department_manager_individual_performance_does_not_grant_measurement():
    who = identity("department_manager")
    assert precheck(individual("PerformanceRating"), who, POLICY) is None
    assert precheck(individual("JobSatisfaction"), who, POLICY).status == "blocked"


def test_ordinary_hr_individual_basic_does_not_grant_statistics():
    who = identity("ordinary_hr")
    assert precheck(individual("Age", "YearsAtCompany"), who, POLICY) is None
    assert precheck(aggregate("Age"), who, POLICY).status == "blocked"
    assert precheck(aggregate("YearsAtCompany"), who, POLICY).status == "blocked"
    assert precheck(StructuredQuery(metric="count"), who, POLICY).status == "blocked"
    assert precheck(aggregate("PerformanceRating"), who, POLICY) is None


@pytest.mark.parametrize("query", [
    StructuredQuery(metric="count"),
    StructuredQuery(metric="count", group_by=["Department"]),
    aggregate(),
    individual("YearsAtCompany"),
])
def test_employee_has_no_hr_data_access(query):
    assert precheck(query, identity("employee"), POLICY).status == "blocked"


@pytest.mark.parametrize("role", ["vp", "department_manager", "department_hr_manager", "ordinary_hr"])
def test_department_scope_requires_server_side_assignment(role):
    query = aggregate("PerformanceRating")
    decision = precheck(query, identity(role, departments=()), POLICY)
    assert decision is not None and decision.rule == "L1.scope"


def test_ordinary_hr_scope_must_be_an_hr_department():
    decision = precheck(aggregate("PerformanceRating"),
                        identity("ordinary_hr", departments=("Sales",)), POLICY)
    assert decision is not None and decision.rule == "L1.scope"


@pytest.mark.parametrize("filt", [
    {"col": "Department", "op": "=", "val": "Research & Development"},
    {"col": "Department", "op": "in", "val": ["Sales", "Research & Development"]},
])
def test_explicit_department_filter_cannot_expand_assignment(filt):
    query = aggregate(filters=[filt])
    decision = precheck(query, identity("department_manager"), POLICY)
    assert decision is not None and decision.rule == "L1.scope"


@pytest.mark.parametrize("departments", [None, set(), {"Research & Development"},
                                          {"Sales", "Research & Development"}])
def test_gate_rechecks_aggregate_population_scope(departments):
    decision = check(aggregate(), identity("department_manager"),
                     [cell(departments=departments)], [], {}, POLICY)
    assert decision.status == "blocked"
    assert evidence.public_view(decision)["cells"] == []


def test_gate_accepts_aggregate_population_inside_scope():
    decision = check(aggregate(), identity("department_manager"),
                     [cell(departments={"Sales"})], [], {}, POLICY)
    assert decision.status == "allowed"


@pytest.mark.parametrize("role", ["ceo", "hr_head", "hr_salary_manager"])
def test_company_scope_is_not_limited_by_the_users_home_department(role):
    decision = check(aggregate(), identity(role, departments=("Sales",)),
                     [cell(departments={"Sales", "Research & Development"})], [], {}, POLICY)
    assert decision.status == "allowed"


@pytest.mark.parametrize("n", [4, 6])
def test_aggregate_response_cannot_smuggle_unrequested_group_fields(n):
    unexpected = cell(n=n)
    unexpected.key = {"MaritalStatus": "Married"}
    decision = check(aggregate(), identity("ceo"), [unexpected], [], {}, POLICY)
    assert decision.status == "blocked"
    public = evidence.public_view(decision)
    assert public["cells"] == []
    assert public["hidden"] == []
    assert "Married" not in str(public)


@pytest.mark.parametrize("query", [
    aggregate(group_by=["AgeBand"]),
    aggregate(filters=[{"col": "AgeBand", "op": "=", "val": "25-29"}]),
])
def test_age_derived_grouping_and_filtering_inherit_sensitive_permissions(query):
    assert precheck(query, identity("ceo"), POLICY).status == "blocked"
    assert precheck(query, identity("hr_head"), POLICY) is None


@pytest.mark.parametrize("query", [
    aggregate("PerformanceRating", group_by=["Department"]),
    aggregate("PerformanceRating", filters=[{"col": "JobLevel", "op": "=", "val": 2}]),
])
def test_individual_basic_grant_does_not_authorize_aggregate_filters(query):
    assert precheck(query, identity("ordinary_hr"), POLICY).status == "blocked"


@pytest.mark.parametrize("role", ["manager", "hr", "hr_head"])
def test_bare_role_strings_are_not_trusted_identities(role):
    decision = precheck(aggregate(), role, POLICY)
    assert decision is not None and decision.status == "blocked"


@pytest.mark.parametrize("field", ["MaritalStatus", "Over18", "Department"])
@pytest.mark.parametrize("metric", ["avg", "median"])
def test_numeric_statistics_require_applicable_fields(field, metric):
    query = StructuredQuery(metric=metric, target=field)
    decision = precheck(query, identity("hr_head"), POLICY)
    assert decision is not None and decision.rule == "L1.metric"


def test_individual_read_does_not_weaken_small_mean_protection():
    who = identity("hr_head")
    assert precheck(individual("MonthlyIncome"), who, POLICY) is None
    decision = check(aggregate(), who, [cell(n=4)], [], {}, POLICY)
    assert decision.status == "blocked" and decision.rule == "L2.min_n"
    assert evidence.public_view(decision)["cells"] == []


def test_config_cannot_lower_five_person_release_floor():
    policy = {**POLICY, "k_threshold": 2}
    decision = check(aggregate(), identity("hr_head"), [cell(n=4)], [], {}, policy)
    assert decision.status == "blocked" and decision.rule == "L2.min_n"


@pytest.mark.parametrize("n, status", [(9, "blocked"), (10, "allowed")])
def test_median_requires_its_own_release_threshold(n, status):
    query = StructuredQuery(metric="median", target="MonthlyIncome")
    policy = {**POLICY, "k_threshold": 2, "median_threshold": 2}
    decision = check(query, identity("hr_head"), [cell(n=n)], [], {}, policy)
    assert decision.status == status


@pytest.mark.parametrize("new_metric, old_metric", [("median", "avg"), ("avg", "median"),
                                                       ("median", "median")])
def test_overlapping_median_history_is_not_treated_as_a_linear_sum(new_metric, old_metric):
    query = StructuredQuery(metric=new_metric, target="MonthlyIncome")
    old = Release(set(range(10)), "MonthlyIncome", old_metric,
                  datetime(2026, 11, 9, tzinfo=timezone.utc), {}, "Earlier statistic")
    decision = check(query, identity("hr_head"), [cell(n=10)], [old], {}, POLICY)
    assert decision.status == "blocked" and decision.rule == "L4.nonlinear"


def test_median_history_connected_through_another_release_still_blocks():
    when = datetime(2026, 11, 9, tzinfo=timezone.utc)
    history = [Release(set(range(10)), "MonthlyIncome", "median", when, {}, "Median"),
               Release(set(range(5, 15)), "MonthlyIncome", "sum", when, {}, "Bridge")]
    new_cell = cell(n=10)
    new_cell.ids = set(range(10, 20))
    decision = check(aggregate(), identity("hr_head"), [new_cell], history, {}, POLICY)
    assert decision.status == "blocked" and decision.rule == "L4.nonlinear"


def test_disjoint_median_history_does_not_block_an_unrelated_population():
    old = Release(set(range(20, 30)), "MonthlyIncome", "median",
                  datetime(2026, 11, 9, tzinfo=timezone.utc), {}, "Unrelated median")
    decision = check(aggregate(), identity("hr_head"), [cell(n=10)], [old], {}, POLICY)
    assert decision.status == "allowed"


def test_individual_release_contains_only_authorized_values():
    query = individual("Age", "Over18", "StockOptionLevel")
    values = {"Age": 34, "Over18": "Y", "StockOptionLevel": 1}
    records = [IndividualRecord(7, "Human Resources", values)]
    decision = check_individual(query, identity("ordinary_hr"), records, POLICY)
    assert decision.status == "allowed"
    public = evidence.public_view(decision)
    assert public["records"] == [{"request_index": 0, "values": values}]
    assert "employee_number" not in str(public)
    assert "department" not in str(public)


@pytest.mark.parametrize("record", [
    IndividualRecord(7, "Sales", {"Age": 34}),
    IndividualRecord(8, "Human Resources", {"Age": 34}),
    IndividualRecord(7, "Human Resources", {"Age": 34, "MonthlyIncome": 5000}),
])
def test_individual_release_rechecks_scope_requested_person_and_fields(record):
    decision = check_individual(individual("Age"), identity("ordinary_hr"), [record], POLICY)
    assert decision.status == "blocked"
    assert not evidence.public_view(decision).get("records")


def test_individual_performance_authorized_for_own_department_only():
    query = individual("PerformanceRating")
    who = identity("department_manager")
    own = IndividualRecord(7, "Sales", {"PerformanceRating": 3})
    other = IndividualRecord(7, "Research & Development", {"PerformanceRating": 3})
    assert check_individual(query, who, [own], POLICY).status == "allowed"
    assert check_individual(query, who, [other], POLICY).status == "blocked"


@pytest.mark.parametrize("field", FIELDS)
def test_department_hr_manager_cannot_release_individual_values_even_within_scope(field):
    record = IndividualRecord(7, "Sales", {field: 3})
    decision = check_individual(individual(field), identity("department_hr_manager"),
                                [record], POLICY)
    assert decision.status == "blocked" and decision.rule == "L1.role"
    assert not evidence.public_view(decision).get("records")


def test_individual_results_identify_request_positions_despite_missing_or_unordered_rows():
    query = IndividualQuery(mode="individual", employee_numbers=[7, 8, 9], fields=["Age"])
    records = [IndividualRecord(9, "Human Resources", {"Age": 42}),
               IndividualRecord(7, "Human Resources", {"Age": 31})]
    decision = check_individual(query, identity("ordinary_hr"), records, POLICY)
    assert evidence.public_view(decision)["records"] == [
        {"request_index": 2, "values": {"Age": 42}},
        {"request_index": 0, "values": {"Age": 31}},
    ]


def test_duplicate_individual_request_ids_are_rejected():
    with pytest.raises(ValidationError, match="unique"):
        IndividualQuery(mode="individual", employee_numbers=[7, 7], fields=["Age"])


def test_hr_salary_manager_can_read_authorized_individual_fields_company_wide():
    query = individual("MonthlyIncome", "StockOptionLevel")
    who = identity("hr_salary_manager", departments=("Human Resources",))
    record = IndividualRecord(7, "Research & Development",
                              {"MonthlyIncome": 5000, "StockOptionLevel": 2})
    assert check_individual(query, who, [record], POLICY).status == "allowed"


@pytest.mark.parametrize("extra", [{"role": "hr_head"}, {"departments": ["Sales"]},
                                     {"scope": "company"}])
def test_model_cannot_supply_its_own_authorization(extra):
    with pytest.raises(ValidationError):
        StructuredQuery(metric="avg", target="MonthlyIncome", **extra)
    with pytest.raises(ValidationError):
        IndividualQuery(mode="individual", employee_numbers=[7], fields=["Age"], **extra)


def test_old_flat_policy_is_not_a_compatibility_permission_bypass():
    legacy = deepcopy(POLICY)
    legacy.pop("schema_version")
    legacy["roles"] = {"hr_head": {"targets": ["*"], "metrics": ["count", "avg"]}}
    assert precheck(aggregate(), identity("hr_head"), legacy).status == "blocked"


def test_sensitive_category_counts_are_not_treated_as_sums_or_ignored():
    from app.memory.releases import release_targets

    query = StructuredQuery(metric="count", group_by=["AgeBand"])
    assert release_targets(query, POLICY) == ["AgeBand"]
    counted = Cell({"AgeBand": "30-34"}, set(range(6)), 6, None)
    who = identity("hr_head")
    assert check(query, who, [counted], [], {}, POLICY).status == "allowed"
    old = Release(set(range(6)), "AgeBand", "count",
                  datetime(2026, 11, 9, tzinfo=timezone.utc), {}, "Earlier count")
    decision = check(query, who, [counted], [old], {}, POLICY)
    assert decision.status == "blocked" and decision.rule == "L4.nonlinear"
