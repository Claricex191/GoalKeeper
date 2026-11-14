"""Offline reset orchestration: rollback, baseline restoration and preserved audit."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from bson import ObjectId
import pytest

from app import clock
from app.demo import baseline, reset


class Transaction:
    def __init__(self, state):
        self.state = state
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def with_transaction(self, callback):
        before = deepcopy(self.state)
        self.in_transaction = True
        try:
            return callback(self)
        except Exception:
            self.state.clear()
            self.state.update(before)
            raise
        finally:
            self.in_transaction = False


@pytest.fixture
def demo(monkeypatch):
    originals = [
        {"EmployeeNumber": 1408, "MonthlyIncome": 16800, "updated_at": {}},
        {"EmployeeNumber": 473, "MonthlyIncome": 12500, "updated_at": {},
         "start_date": clock.NEW_HIRE_START},
    ]
    state = {
        "offset": 3, "employees": deepcopy(originals), "releases": [{"old": True}],
        "precedents": [{"old": True}],
        "ledger": [{"_id": ObjectId(), "kind": "old_audit"}], "meta": {"ledger_after": None},
        "approvals": [{"kind": "review"}, {"_id": "kill_switch", "active": True},
                      {"_id": "unrelated_control"}],
    }
    state["employees"][0].update(MonthlyIncome=18500, updated_at={"MonthlyIncome": clock.NEW_HIRE_START})
    db = SimpleNamespace(client=SimpleNamespace(start_session=lambda: Transaction(state)))
    sessions = []

    def record(session):
        assert session.in_transaction
        sessions.append(session)

    def find(_query, _projection, *, session):
        record(session)
        return deepcopy(state["employees"])

    def replace(query, doc, *, session):
        record(session)
        for i, current in enumerate(state["employees"]):
            if current["EmployeeNumber"] == query["EmployeeNumber"]:
                state["employees"][i] = deepcopy(doc)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    db.employees = SimpleNamespace(find=Mock(side_effect=find), replace_one=Mock(side_effect=replace))
    for name in ("releases", "precedents", "approvals"):
        def delete(query, *, session, collection=name):
            record(session)
            state[collection][:] = [doc for doc in state[collection]
                                   if not all(doc.get(k) == v for k, v in query.items())]
        setattr(db, name, SimpleNamespace(delete_many=Mock(side_effect=delete)))

    def switch(query, update, *, session):
        record(session)
        assert query == {"_id": "kill_switch"}
        next(doc for doc in state["approvals"] if doc.get("_id") == "kill_switch").update(update["$set"])

    db.approvals.update_one = Mock(side_effect=switch)
    def latest(_query, _projection, *, sort, session):
        record(session)
        assert sort == [("_id", -1)]
        return max(state["ledger"], key=lambda doc: doc["_id"]) if state["ledger"] else None

    def cutoff(query, update, *, upsert, session):
        record(session)
        assert query == {"_id": "demo_reset"} and upsert
        state["meta"].update(update["$set"])

    db.ledger = SimpleNamespace(delete_many=Mock(side_effect=AssertionError("append-only ledger")),
                                find_one=Mock(side_effect=latest))
    db.meta = SimpleNamespace(update_one=Mock(side_effect=cutoff))

    def rewind(*, db, session):
        record(session)
        state["offset"] = 0
        return clock.DEMO_START

    def seed(_db, *, session):
        record(session)
        assert state["employees"] == originals and state["offset"] == 0
        assert not state["releases"] and not state["precedents"]
        state["releases"].extend([{"team": "sales"}, {"team": "hr"}])
        state["ledger"].extend([{"_id": ObjectId(), "kind": "gate_decision"} for _ in range(2)])
        return 2

    monkeypatch.setattr(reset, "load_baseline", lambda: deepcopy(originals))
    monkeypatch.setattr(reset.store, "load_active", Mock(return_value={"version": 1}))
    monkeypatch.setattr(reset.store, "clear_cache", Mock())
    monkeypatch.setattr(reset.executor, "clear_cache", Mock())
    monkeypatch.setattr(clock, "reset", rewind)
    monkeypatch.setattr(reset, "run_warmup", Mock(side_effect=seed))
    return db, state, originals, sessions


def test_reset_restores_only_changed_employee_and_keeps_audit(demo):
    db, state, originals, sessions = demo
    assert reset.reset_demo(db) == clock.DEMO_START
    assert state["employees"] == originals
    assert db.employees.replace_one.call_count == 1
    assert state["releases"] == [{"team": "sales"}, {"team": "hr"}]
    assert state["approvals"] == [{"_id": "kill_switch", "active": False},
                                  {"_id": "unrelated_control"}]
    assert [entry["kind"] for entry in state["ledger"]] == ["old_audit", "gate_decision", "gate_decision"]
    assert state["meta"]["ledger_after"] == state["ledger"][0]["_id"]
    db.ledger.delete_many.assert_not_called()
    assert all(session is sessions[0] for session in sessions)


def test_failed_warmup_rolls_back_entire_reset(demo):
    db, state, _, _ = demo
    before = deepcopy(state)
    reset.run_warmup.side_effect = RuntimeError("ledger write failed")
    with pytest.raises(RuntimeError, match="ledger write failed"):
        reset.reset_demo(db)
    assert state == before
    reset.executor.clear_cache.assert_called()
    reset.store.clear_cache.assert_called()


def test_incomplete_employee_data_leaves_previous_demo_intact(demo):
    db, state, _, _ = demo
    state["employees"].pop()
    before = deepcopy(state)
    with pytest.raises(LookupError, match="do not match"):
        reset.reset_demo(db)
    assert state == before
    reset.run_warmup.assert_not_called()


def test_missing_baseline_cannot_start_a_reset(demo, monkeypatch):
    db, state, _, _ = demo
    before = deepcopy(state)
    monkeypatch.setattr(reset, "load_baseline", Mock(side_effect=LookupError("baseline missing")))
    with pytest.raises(LookupError, match="baseline missing"):
        reset.reset_demo(db)
    assert state == before
    db.employees.find.assert_not_called()


def test_reset_replaces_stale_cutoff_when_ledger_empty(demo):
    db, state, _, _ = demo
    state["ledger"].clear()
    state["meta"]["ledger_after"] = ObjectId()
    reset.reset_demo(db)
    assert state["meta"]["ledger_after"] is None


def test_legacy_policy_cannot_start_reset(demo):
    db, state, _, _ = demo
    before = deepcopy(state)
    reset.store.load_active.side_effect = ValueError("policy schema_version must be 2")
    with pytest.raises(LookupError, match="active v2 policy"):
        reset.reset_demo(db)
    assert state == before
    db.employees.find.assert_not_called()


def test_baseline_missing_is_local_error(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline, "CSV_PATH", tmp_path / "missing.csv")
    with pytest.raises(LookupError, match="local employee baseline"):
        baseline.load_baseline()


def test_baseline_cache_cannot_be_modified_by_caller(tmp_path, monkeypatch):
    path = tmp_path / "baseline.csv"
    path.write_text("placeholder")
    monkeypatch.setattr(baseline, "CSV_PATH", path)
    cached = ({"EmployeeNumber": 1, "updated_at": {}},)
    monkeypatch.setattr(baseline, "_read_baseline", lambda *_a: cached)
    first = baseline.load_baseline()
    first[0]["updated_at"]["MonthlyIncome"] = clock.NEW_HIRE_START
    assert baseline.load_baseline()[0]["updated_at"] == {}


def test_transaction_reads_active_policy_without_using_or_polluting_cache(monkeypatch):
    from app.policy import store

    old = {"version": 1, "schema_version": 2}
    active = {"version": 2, "schema_version": 2}
    monkeypatch.setattr(store, "_active_cache", {"ibm_hr": old})
    db = SimpleNamespace(policies=SimpleNamespace(find_one=Mock(return_value=active)))
    session = object()
    assert store.load_active(db, "ibm_hr", session=session) is active
    db.policies.find_one.assert_called_once_with(
        {"dataset": "ibm_hr", "status": "active"}, session=session)
    assert store._active_cache["ibm_hr"] is old


def test_transaction_cannot_bypass_policy_schema_check(monkeypatch):
    from app.policy import store

    monkeypatch.setattr(store, "_active_cache", {"ibm_hr": {"schema_version": 2}})
    db = SimpleNamespace(policies=SimpleNamespace(find_one=Mock(return_value={"version": 1})))
    with pytest.raises(ValueError, match="schema_version must be 2"):
        store.load_active(db, "ibm_hr", session=object())
