"""Demo user directory: case files agree with it, and the API resolves identity server-side."""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.users import DEMO_USERS, current_identity, require_role

CASES = json.loads((Path(__file__).parent / "cases" / "gate_cases.json").read_text())


@pytest.mark.parametrize("step", [s for c in CASES for s in c["steps"] if "user" in s],
                         ids=lambda s: f'{s["user"]}@{s["date"]}')
def test_case_users_match_directory(step):
    who = DEMO_USERS.get(step["user"])
    assert who is not None, f'{step["user"]} is not in app.users.DEMO_USERS'
    assert who.team == step["team"], f'{who.user} is in team {who.team}, case says {step["team"]}'


def test_known_user_resolves():
    assert current_identity("mgr_2").team == "sales"


@pytest.mark.parametrize("header", [None, "", "nobody", "MGR_1"])
def test_unknown_user_rejected(header):
    with pytest.raises(HTTPException) as e:
        current_identity(header)
    assert e.value.status_code == 401


def test_manager_cannot_review():
    with pytest.raises(HTTPException) as e:
        require_role(DEMO_USERS["mgr_1"], "hr_head")
    assert e.value.status_code == 403
    require_role(DEMO_USERS["hr_1"], "hr_head")


def test_directory_covers_every_permission_role():
    from app.policy.defaults import POLICY
    assert {who.role for who in DEMO_USERS.values()} == set(POLICY["roles"])


def test_department_assignment_is_distinct_from_memory_team():
    who = current_identity("mgr_3")
    assert who.role == "department_manager"
    assert who.departments == ("Research & Development",)
    assert who.team == "rnd"
    assert current_identity("ordinary_hr_1").departments == ("Human Resources",)
