"""Security memory (releases) against Atlas. Skipped without Atlas.

Every test run uses its own random project and deletes it afterwards, and passes times
explicitly, so it never touches demo memory or the shared clock. Uses its own policy
dict, so changes to the seeded policy do not break these tests.
"""
import uuid
from datetime import timedelta

import pytest
from pymongo.errors import PyMongoError

from app.clock import DEMO_START, NEW_HIRE_START
from app.memory.releases import (load_history, make_label, record_then_release,
                                 release_targets, retention_days)
from app.query.executor import run_query
from app.schemas import Cell, Decision, StructuredQuery
from app.users import DEMO_USERS

MON, THU = DEMO_START, NEW_HIRE_START
MGR1, MGR2, MGR3 = DEMO_USERS["mgr_1"], DEMO_USERS["mgr_2"], DEMO_USERS["mgr_3"]

POLICY = {
    "version": 1,
    "sensitive_targets": ["MonthlyIncome", "PerformanceRating", "Attrition", "JobSatisfaction"],
    "memory": {"scope": "team", "retention_days": 365,
               "retention_by_target": {"MonthlyIncome": 730}},
}
SALES_MGR_AVG = StructuredQuery(
    filters=[{"col": "Department", "op": "=", "val": "Sales"},
             {"col": "JobRole", "op": "=", "val": "Manager"}],
    metric="avg", target="MonthlyIncome")


# --- pure helpers: no database ------------------------------------------------------
def test_label_has_question_not_values():
    assert make_label(SALES_MGR_AVG, {}) == \
        "Department=Sales, JobRole=Manager | average MonthlyIncome"
    grouped = StructuredQuery(filters=[{"col": "JobLevel", "op": "in", "val": [4, 5]}],
                              group_by=["Gender"], metric="count")
    assert make_label(grouped, {"Gender": "Female"}) == \
        "JobLevel in [4, 5] | Gender=Female | count"


def test_release_targets():
    assert release_targets(SALES_MGR_AVG, POLICY) == ["MonthlyIncome"]
    attrition = StructuredQuery(filters=[{"col": "Attrition", "op": "=", "val": "Yes"}],
                                metric="count")
    assert release_targets(attrition, POLICY) == ["Attrition"]
    plain = StructuredQuery(filters=[{"col": "Department", "op": "=", "val": "Sales"}],
                            metric="count")
    assert release_targets(plain, POLICY) == []


def test_retention_per_target():
    assert retention_days(POLICY, "MonthlyIncome") == 730
    assert retention_days(POLICY, "JobSatisfaction") == 365


# --- database -----------------------------------------------------------------------
@pytest.fixture(scope="module")
def db():
    try:
        from app.db import ensure_indexes, get_db
        d = get_db()
        d.client.admin.command("ping", maxTimeMS=3000)
    except Exception as e:
        pytest.skip(f"Atlas not reachable: {e}")
    ensure_indexes(d)
    return d


@pytest.fixture
def project(db):
    p = f"test-{uuid.uuid4().hex[:8]}"
    yield p
    db.releases.delete_many({"project": p})
    db.ledger.delete_many({"project": p})


def entry(project, who, status="allowed"):
    return {"kind": "gate_decision", "time": MON, "user": who.user, "role": who.role,
            "team": who.team, "project": project, "session_id": "s1",
            "query": SALES_MGR_AVG.model_dump(), "status": status, "policy_version": 1}


def record_monday(db, project):
    cells = run_query(db, SALES_MGR_AVG, MON)
    n = record_then_release(db, Decision(status="allowed", cells=cells), SALES_MGR_AVG,
                            MGR1, project, "s1", MON, POLICY, entry(project, MGR1))
    assert n == 1
    return cells[0]


def test_written_and_read_back_on_thursday(db, project):
    cell = record_monday(db, project)
    (r,) = load_history(db, MGR1, project, ["MonthlyIncome"], THU, POLICY)
    assert r.ids == cell.ids and len(r.ids) == 36
    assert r.created_at == MON and r.metric == "avg"
    assert r.label == "Department=Sales, JobRole=Manager | average MonthlyIncome"
    assert db.ledger.count_documents({"project": project}) == 1
    stored = db.releases.find_one({"project": project})
    assert "value" not in stored                      # data minimisation: no released values


def test_team_scope_shares_and_isolates(db, project):
    record_monday(db, project)
    assert len(load_history(db, MGR2, project, ["MonthlyIncome"], THU, POLICY)) == 1  # same team
    assert load_history(db, MGR3, project, ["MonthlyIncome"], THU, POLICY) == []      # other team
    user_scope = {**POLICY, "memory": {**POLICY["memory"], "scope": "user"}}
    assert load_history(db, MGR2, project, ["MonthlyIncome"], THU, user_scope) == []


def test_other_target_not_returned(db, project):
    record_monday(db, project)
    assert load_history(db, MGR1, project, ["JobSatisfaction"], THU, POLICY) == []
    assert load_history(db, MGR1, project, [], THU, POLICY) == []


def test_expiry_uses_policy_retention(db, project):
    record_monday(db, project)
    assert len(load_history(db, MGR1, project, ["MonthlyIncome"],
                            MON + timedelta(days=729), POLICY)) == 1
    assert load_history(db, MGR1, project, ["MonthlyIncome"],
                        MON + timedelta(days=731), POLICY) == []


def test_small_cell_rejected_and_nothing_written(db, project):
    """Defense in depth: even if the gate wrongly allowed n < 5, the validator refuses,
    and the transaction rolls back the ledger entry too."""
    tiny = Cell(key={}, ids={1, 2, 3}, n=3, value=1.0)
    with pytest.raises(PyMongoError):
        record_then_release(db, Decision(status="allowed", cells=[tiny]), SALES_MGR_AVG,
                            MGR1, project, "s1", MON, POLICY, entry(project, MGR1))
    assert db.releases.count_documents({"project": project}) == 0
    assert db.ledger.count_documents({"project": project}) == 0


def test_nothing_to_remember_still_audited(db, project):
    plain = StructuredQuery(filters=[{"col": "Department", "op": "=", "val": "Sales"}],
                            metric="count")
    cells = run_query(db, plain, MON)
    n = record_then_release(db, Decision(status="allowed", cells=cells), plain, MGR1,
                            project, "s1", MON, POLICY, entry(project, MGR1))
    assert n == 0
    assert db.releases.count_documents({"project": project}) == 0
    assert db.ledger.count_documents({"project": project}) == 1
