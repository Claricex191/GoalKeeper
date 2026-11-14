"""The system prompt describes the server-resolved role, scope and grants."""

from app.agent import prompts
from app.policy.defaults import POLICY
from app.users import DEMO_USERS


def test_department_manager_scope_and_grants():
    text = prompts.describe_user(DEMO_USERS["mgr_1"], POLICY)
    assert "Authorized departments: Sales." in text
    assert "- salary: statistics only" in text
    assert "- performance ratings: statistics and individual records" in text
    assert "sensitive basic information (age" in text and "): no access" in text


def test_company_wide_and_individual_only():
    assert "the whole company" in prompts.describe_user(DEMO_USERS["ceo_1"], POLICY)
    text = prompts.describe_user(DEMO_USERS["ordinary_hr_1"], POLICY)
    assert "Human Resources" in text
    assert "basic information (department, job role, level, gender, tenure...): " \
           "individual records only (no statistics)" in text


def test_department_hr_manager_has_only_scoped_statistics():
    text = prompts.describe_user(DEMO_USERS["dept_hr_1"], POLICY)
    assert "Authorized departments: Sales." in text
    assert text.count(": statistics only") == 5
    assert "individual records" not in text


def test_employee_has_no_access():
    assert "no access to HR data" in prompts.describe_user(DEMO_USERS["emp_1"], POLICY)


def test_prompt_no_longer_denies_individual_access():
    text = prompts.system_prompt(DEMO_USERS["hr_1"], POLICY)
    assert "never will" not in text
    assert "query_hr_individual" in text
