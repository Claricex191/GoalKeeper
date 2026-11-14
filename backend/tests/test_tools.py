"""agent.tools: tool schema (pure) and handle_query_hr end-to-end against Atlas.

Atlas tests use a random project, pass `now` explicitly (never touch the shared demo
clock) and delete their releases and ledger rows afterwards. They use the ACTIVE policy,
so they also check that the seeded policy still supports the demo.
"""
import json
import uuid

import pytest

from app.agent.tools import DISPATCH, handle_query_hr, tool_specs
from app.clock import DEMO_START, NEW_HIRE_START
from app.policy import store
from app.policy.defaults import POLICY
from app.users import DEMO_USERS

MON, THU = DEMO_START, NEW_HIRE_START
MGR1, MGR2 = DEMO_USERS["mgr_1"], DEMO_USERS["mgr_2"]       # both team sales

SALES_MGR = {"filters": [{"col": "Department", "op": "=", "val": "Sales"},
                         {"col": "JobRole", "op": "=", "val": "Manager"}],
             "group_by": [], "metric": "avg", "target": "MonthlyIncome", "transform": None}
SALES_DEPT = {"filters": [{"col": "Department", "op": "=", "val": "Sales"}],
              "group_by": [], "metric": "avg", "target": "MonthlyIncome",
              "transform": "T3_widen_scope"}




# --- schema: no database ----------------------------------------------------------------
def _all_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for v in schema.values():
            yield from _all_objects(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _all_objects(v)


@pytest.mark.parametrize("who", [MGR1, DEMO_USERS["hr_1"], DEMO_USERS["ordinary_hr_1"]])
def test_schema_is_strict_compatible(who):
    specs = tool_specs(POLICY, who)
    assert specs
    for spec in specs:
        assert spec["strict"] is True
        for obj in _all_objects(spec["input_schema"]):
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])
        text = json.dumps(spec)
        for unsupported in ("minimum", "maximum", "minLength", "maxLength", "maxItems"):
            assert unsupported not in text


def test_schema_is_narrowed_by_role():
    s = store.tool_schema(POLICY, "department_manager")["properties"]
    text = json.dumps(s)
    for field in ("MonthlyIncome", "PerformanceRating", "YearsAtCompany"):
        assert field in text
    for forbidden in ("AgeBand", "StockOptionLevel", "MaritalStatus"):
        assert forbidden not in text
    assert {"count", "avg", "median", "max", "min"} <= set(s["metric"]["enum"])
    assert "AgeBand" not in s["group_by"]["items"].get("enum", [])
    ordinary = store.tool_schema(POLICY, "ordinary_hr")["properties"]
    assert "count" not in ordinary["metric"]["enum"]
    assert "MonthlyIncome" not in json.dumps(ordinary)
    assert "PerformanceRating" in json.dumps(ordinary)
    assert tool_specs(POLICY, DEMO_USERS["emp_1"]) == []
    assert tool_specs(POLICY, "nobody") == []



# --- handle_query_hr against Atlas --------------------------------------------------------
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
    db.approvals.delete_many({"project": p})
    db.precedents.delete_many({"project": p})


def ctx(db, project, who, now, memory=True, turn=None):
    return {"db": db, "who": who, "project": project, "session_id": "s-test",
            "memory": memory, "policy": store.load_active(db, "ibm_hr"), "now": now,
            "turn_id": turn}


def _no_ids(out):
    text = json.dumps(out, default=str)
    for leak in ("ids", "vmin", "vmax", "EmployeeNumber"):
        assert leak not in text, leak


def test_act1_new_hire(db, project):
    # Monday: mgr_1 learns the Sales-manager average (36 people), remembered for the team
    mon = handle_query_hr(ctx(db, project, MGR1, MON), SALES_MGR)
    assert mon["trace"]["status"] == "allowed"
    assert mon["for_llm"]["cells"] == [{"group": {}, "n": 35, "value": 16986.46}]
    assert db.releases.count_documents({"project": project}) == 1
    _no_ids(mon)

    # Thursday, control panel: no memory → the differencing attack succeeds
    off = handle_query_hr(ctx(db, project, MGR2, THU, memory=False), SALES_MGR)
    assert off["for_llm"]["cells"] == [{"group": {}, "n": 36, "value": 16861.94}]
    assert db.releases.count_documents({"project": project}) == 1   # nothing written

    # Thursday, with memory: a teammate asks again → blocked, rewrite offered
    on = DISPATCH["query_hr"](ctx(db, project, MGR2, THU), SALES_MGR)
    assert on["trace"]["status"] == "blocked"
    assert on["trace"]["rule"] == "L4.pairwise"
    assert on["for_llm"]["cells"] == []
    assert "T3_widen_scope" in on["for_llm"]["allowed_transforms"]
    assert on["trace"]["evidence_text"]
    _no_ids(on)

    # the rewrite the LLM is expected to make: the whole Sales department
    wide = handle_query_hr(ctx(db, project, MGR2, THU), SALES_DEPT)
    assert wide["trace"]["status"] == "allowed"
    assert wide["for_llm"]["cells"] == [{"group": {}, "n": 355, "value": 7260.26}]
    assert wide["trace"]["query"]["transform"] == "T3_widen_scope"

    kinds = [(d["status"], d["memory_on"]) for d in
             db.ledger.find({"project": project}).sort("time", 1)]
    assert kinds == [("allowed", True), ("allowed", False), ("blocked", True),
                     ("allowed", True)]


def test_extreme_is_refused_before_the_data(db, project):
    q = {**SALES_MGR, "metric": "max"}
    out = handle_query_hr(ctx(db, project, MGR1, MON), q)
    assert out["trace"]["status"] == "blocked" and out["trace"]["rule"] == "L1.extreme"
    assert out["for_llm"]["allowed_transforms"].keys() == {"T4_extreme_to_avg"}


def test_bad_input_and_bad_values_come_back_as_errors(db, project):
    out = handle_query_hr(ctx(db, project, MGR1, MON), {"metric": "var"})
    assert out["trace"]["status"] == "error" and "Invalid query" in out["for_llm"]["message"]
    q = {**SALES_MGR, "filters": [{"col": "JobRole", "op": ">", "val": ["a", "b"]}]}
    out = handle_query_hr(ctx(db, project, MGR1, MON), q)
    assert out["trace"]["status"] == "error"
    assert db.ledger.count_documents({"project": project, "status": "error"}) == 2


def test_l5_probing_goes_to_reviewer(db, project):
    from app.review import approvals

    def ask(turn, q=SALES_MGR, who=MGR1, memory=True):
        c = ctx(db, project, who, MON, memory, turn=turn)
        c["policy"] = {**c["policy"], "probing": {"max_attempts": 5, "window_days": 7}}
        return handle_query_hr(c, q)["trace"]

    # turn 0: first block (max is refused, L1.extreme); the agent's rewrite in the
    # same message is not a new question
    assert ask("t0", {**SALES_MGR, "metric": "max"})["rule"] == "L1.extreme"
    assert ask("t0", {**SALES_MGR, "transform": "T4_extreme_to_avg"})["status"] == "allowed"
    # turns 1-5: five more questions on MonthlyIncome, answered normally;
    # a rewrite inside turn 5 is still answered (never cut off midway)
    for t in range(1, 6):
        assert ask(f"t{t}")["rule"] is None
    assert ask("t5", SALES_DEPT)["status"] == "allowed"
    # control panel and other users are never counted or locked
    assert ask("off", memory=False)["status"] == "allowed"
    assert ask("m2", who=MGR2)["status"] == "allowed"
    # turn 6 goes to a reviewer, no data
    sixth = ask("t6")
    assert sixth["rule"] == "L5.probing" and sixth["cells"] == []
    aid = sixth["approval_id"]
    assert [p["id"] for p in approvals.list_pending(db, project)] == [aid]
    _no_ids(sixth)
    # locked until decided, for this user and field only
    assert ask("t7")["rule"] == "L5.pending"
    assert ask("m3", who=MGR2)["status"] == "allowed"
    # the reviewer decides → unlocked, count restarts
    assert approvals.decide(db, aid, False, None, "hr_1", MON)
    assert not approvals.decide(db, aid, True, None, "hr_1", MON)     # already decided
    assert ask("t8")["status"] == "allowed"
    assert approvals.list_pending(db, project) == []
    rules = [d.get("rule") for d in db.ledger.find({"project": project, "user": "mgr_1"})]
    assert rules.count("L5.probing") == 1 and rules.count("L5.pending") == 1
