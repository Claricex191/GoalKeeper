"""Precedents (semantic memory, Atlas Vector Search) through agent.tools.

Voyage is faked (tests/conftest.py); the vector search itself is real Atlas, so
this is skipped without Atlas. New documents take a few seconds to become searchable.
"""
import time
import uuid

import pytest

from app.agent.tools import handle_query_hr
from app.clock import DEMO_START, NEW_HIRE_START
from app.memory.precedents import describe
from app.policy import store
from app.schemas import StructuredQuery
from app.users import DEMO_USERS

MON, THU = DEMO_START, NEW_HIRE_START
SALES_MGR = {"filters": [{"col": "Department", "op": "=", "val": "Sales"},
                         {"col": "JobRole", "op": "=", "val": "Manager"}],
             "group_by": [], "metric": "avg", "target": "MonthlyIncome", "transform": None}
SALES_DEPT = {**SALES_MGR, "filters": SALES_MGR["filters"][:1], "transform": "T3_widen_scope"}


def test_describe_is_the_question_not_values():
    q = StructuredQuery.model_validate(SALES_MGR)
    assert describe(q) == ("average MonthlyIncome where Department = Sales "
                           "and JobRole = Manager")
    g = StructuredQuery(metric="count", group_by=["Gender"],
                        filters=[{"col": "JobLevel", "op": "in", "val": [4, 5]}])
    assert describe(g) == "head count where JobLevel in [4, 5] by Gender"


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
    for c in ("releases", "ledger", "approvals", "precedents"):
        db[c].delete_many({"project": p})


def ask(db, project, who, now, q, memory=True):
    c = {"db": db, "who": DEMO_USERS[who], "project": project, "session_id": "s",
         "memory": memory, "policy": store.load_active(db, "ibm_hr"), "now": now}
    return handle_query_hr(c, q)["trace"]


def test_refusal_is_remembered_and_suggested_later(db, project):
    assert ask(db, project, "mgr_1", MON, SALES_MGR)["status"] == "allowed"
    # Thursday: refused (L4) → stored once, with no values or ids
    assert ask(db, project, "mgr_2", THU, SALES_MGR)["status"] == "blocked"
    assert ask(db, project, "mgr_2", THU, SALES_MGR)["status"] == "blocked"   # duplicate
    docs = list(db.precedents.find({"project": project}))
    assert len(docs) == 1
    assert docs[0]["label"].startswith("average MonthlyIncome where Department = Sales")
    assert docs[0]["transform"] is None and "ids" not in docs[0]
    # the rewrite that is answered is noted on the precedent
    assert ask(db, project, "mgr_2", THU, SALES_DEPT)["status"] == "allowed"
    assert db.precedents.find_one({"project": project})["transform"] == "T3_widen_scope"

    # a teammate asks the same thing later → the hint says what worked
    hints = []
    for _ in range(15):                       # wait for Atlas Search to index the doc
        hints = ask(db, project, "mgr_1", THU, SALES_MGR)["hints"]
        if hints:
            break
        time.sleep(2)
    assert hints and "what worked: ask about a wider population" in hints[0]


def test_control_panel_has_no_semantic_memory(db, project):
    ask(db, project, "mgr_1", MON, SALES_MGR)
    off = ask(db, project, "mgr_2", THU, SALES_MGR, memory=False)
    assert off["status"] == "allowed" and off["hints"] == []
    q = {**SALES_MGR, "metric": "max"}
    assert ask(db, project, "mgr_2", THU, q, memory=False)["status"] == "blocked"
    assert db.precedents.count_documents({"project": project}) == 0
