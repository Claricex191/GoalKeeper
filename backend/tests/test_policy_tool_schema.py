"""Role-specific strict tool schemas for the v2 permission model; no database."""
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.policy.defaults import POLICY
from app.policy.store import tool_schema
from app.schemas import StructuredQuery


AVAILABLE = [
    ("ceo", "aggre_stats"), ("vp", "aggre_stats"),
    ("hr_head", "aggre_stats"), ("hr_head", "individual"),
    ("hr_salary_manager", "aggre_stats"), ("hr_salary_manager", "individual"),
    ("department_hr_manager", "aggre_stats"),
    ("department_manager", "aggre_stats"), ("department_manager", "individual"),
    ("ordinary_hr", "aggre_stats"), ("ordinary_hr", "individual"),
]


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def fields(role):
    return set(tool_schema(POLICY, role, "individual")["properties"]["fields"]["items"]["enum"])


def targets(role):
    target = tool_schema(POLICY, role)["properties"]["target"]
    return set(target["anyOf"][0]["enum"] if "anyOf" in target else target["enum"])


@pytest.mark.parametrize("role, mode", AVAILABLE)
def test_all_offered_tool_schemas_are_strict_compatible(role, mode):
    schema = tool_schema(POLICY, role, mode)
    assert schema["type"] == "object"
    assert schema["properties"]["mode"]["enum"] == [mode]
    for node in nodes(schema):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        if "enum" in node:
            assert node["enum"]  # Empty enums are invalid schema, not a deny mechanism.
    text = json.dumps(schema)
    for unsupported in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        assert unsupported not in text
    for injected in ("role", "scope", "departments"):
        assert injected not in schema["properties"]


@pytest.mark.parametrize("role, mode", AVAILABLE)
def test_offered_schema_is_valid_json_schema(role, mode):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(tool_schema(POLICY, role, mode))


def test_hr_individual_schemas_preserve_the_three_distinct_grants():
    assert {"Age", "StockOptionLevel", "MonthlyIncome", "JobSatisfaction", "PerformanceRating"} <= fields("hr_head")
    salary = fields("hr_salary_manager")
    assert {"Age", "StockOptionLevel", "MonthlyIncome"} <= salary
    assert {"JobSatisfaction", "PerformanceRating"}.isdisjoint(salary)
    ordinary = fields("ordinary_hr")
    assert {"Age", "Over18", "StockOptionLevel", "YearsAtCompany"} <= ordinary
    assert {"MonthlyIncome", "JobSatisfaction", "PerformanceRating"}.isdisjoint(ordinary)


def test_department_hr_manager_aggregate_schema_preserves_all_five_categories():
    assert {"YearsAtCompany", "Age", "StockOptionLevel", "MonthlyIncome",
            "JobSatisfaction", "PerformanceRating"} <= targets("department_hr_manager")


def test_department_manager_individual_schema_contains_only_performance():
    assert fields("department_manager") == {"PerformanceRating"}


@pytest.mark.parametrize("role", ["ceo", "vp", "department_manager"])
def test_non_hr_aggregate_schema_never_offers_sensitive_basic_fields(role):
    schema = tool_schema(POLICY, role)
    props = schema["properties"]
    assert "AgeBand" in props["group_by"]["items"]["enum"]        # basic since 2026 data
    assert "MaritalStatus" not in props["group_by"]["items"]["enum"]
    assert "MaritalStatus" not in props["filters"]["items"]["properties"]["col"]["enum"]
    assert {"Age", "DistanceFromHome", "StockOptionLevel"}.isdisjoint(targets(role))
    assert {"MonthlyIncome", "PerformanceRating"} <= targets(role)


def test_ordinary_hr_statistics_do_not_inherit_individual_basic_permissions():
    props = tool_schema(POLICY, "ordinary_hr")["properties"]
    assert targets("ordinary_hr") == {"PerformanceRating"}
    assert props["metric"]["enum"] == ["avg", "median", "max", "min"]
    assert props["filters"]["items"] == {"type": "null"}
    assert props["group_by"]["items"] == {"type": "null"}


def test_ordinary_hr_empty_filter_schema_is_valid_and_cannot_supply_real_filters():
    jsonschema = pytest.importorskip("jsonschema")
    schema = tool_schema(POLICY, "ordinary_hr")
    payload = {"mode": "aggre_stats", "metric": "avg", "target": "PerformanceRating",
               "filters": [], "group_by": [], "transform": None}
    jsonschema.validate(payload, schema)
    StructuredQuery.model_validate(payload)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**payload, "group_by": ["Department"]}, schema)
    # Null array items avoid invalid empty enums in the strict schema; runtime
    # validation still rejects any nonempty null array before authorization/data.
    for key in ("filters", "group_by"):
        with pytest.raises(ValidationError):
            StructuredQuery.model_validate({**payload, key: [None]})


@pytest.mark.parametrize("role, mode", [
    ("employee", "aggre_stats"), ("employee", "individual"),
    ("ceo", "individual"), ("vp", "individual"),
    ("department_hr_manager", "individual"),
    ("nobody", "aggre_stats"), ("manager", "aggre_stats"),
])
def test_roles_without_access_receive_no_tool_schema(role, mode):
    with pytest.raises(PermissionError):
        tool_schema(POLICY, role, mode)


def test_legacy_flat_grants_are_not_used_for_tool_schemas():
    legacy = deepcopy(POLICY)
    legacy.pop("schema_version")
    legacy["roles"] = {"hr_head": {"targets": ["*"], "metrics": ["count", "avg"]}}
    with pytest.raises(PermissionError):
        tool_schema(legacy, "hr_head")


def test_never_return_and_identifier_labels_exclude_fields_from_both_modes():
    policy = deepcopy(POLICY)
    policy["never_return"].append("Age")
    policy["field_labels"]["MonthlyIncome"] = "identifier"
    for mode in ("aggre_stats", "individual"):
        text = json.dumps(tool_schema(policy, "hr_head", mode))
        for hidden in ('"Age"', '"MonthlyIncome"', '"EmployeeNumber"'):
            assert hidden not in text


def test_count_only_schema_does_not_create_an_empty_target_enum():
    policy = deepcopy(POLICY)
    policy["field_metrics"] = {field: ["count"] for field in policy["field_metrics"]}
    schema = tool_schema(policy, "ceo")
    assert schema["properties"]["metric"]["enum"] == ["count"]
    assert schema["properties"]["target"]["type"] == "null"
