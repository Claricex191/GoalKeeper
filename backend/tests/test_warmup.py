"""Offline checks of warmup guards, real gate decisions and transaction composition."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bson import BSON
from bson.int64 import Int64

from app import clock, gate
from app.demo import warmup
from app.memory import releases
from app.query import executor
from app.schemas import Cell, Decision, Release, StructuredQuery


POLICY = {
    "version": 3, "k_threshold": 5, "sensitive_targets": ["MonthlyIncome"],
    "quasi_identifiers": ["Department", "JobRole"], "never_return": [],
    "field_labels": {"Department": "quasi_identifier", "JobRole": "quasi_identifier",
                     "MonthlyIncome": "sensitive"},
    "roles": {"manager": {"targets": ["MonthlyIncome"], "metrics": ["avg"]}},
    "memory": {"scope": "team", "retention_days": 730},
}


class Session:
    """Rollback-capable test transaction; the production writer is not mocked."""

    def __init__(self, db):
        self.db = db
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def with_transaction(self, callback):
        before = deepcopy((self.db.release_docs, self.db.audit_docs))
        self.in_transaction = True
        try:
            return callback(self)
        except Exception:
            self.db.release_docs[:], self.db.audit_docs[:] = before
            raise
        finally:
            self.in_transaction = False


@pytest.fixture
def demo(monkeypatch):
    db = SimpleNamespace(release_docs=[], audit_docs=[], offset=0, dirty=False, empty=False)
    db.meta = SimpleNamespace(update_one=Mock())
    db.releases = SimpleNamespace(
        find_one=Mock(side_effect=lambda *_a, **_k: next(iter(db.release_docs), None)),
        insert_many=Mock(side_effect=lambda docs, **_k: db.release_docs.extend(deepcopy(docs))),
    )
    db.ledger = SimpleNamespace(
        insert_one=Mock(side_effect=lambda doc, **_k: db.audit_docs.append(deepcopy(doc))))
    db.employees = SimpleNamespace(find_one=Mock(side_effect=lambda query, *_a, **_k:
        ({"_id": 1} if db.dirty else None) if query else (None if db.empty else {"_id": 1})))
    db.client = SimpleNamespace(start_session=Mock(side_effect=lambda: Session(db)))
    monkeypatch.setattr(clock, "offset_days", lambda **_k: db.offset)
    monkeypatch.setattr(clock, "now", lambda **_k: clock.DEMO_START)
    monkeypatch.setattr(warmup.store, "load_active", lambda *_a, **_kw: deepcopy(POLICY))
    cells = {
        "Sales": Cell({}, set(range(1, 7)), 6, 12000.0, 10000.0, 14000.0),
        "Human Resources": Cell({}, set(range(10, 16)) | {1408}, 7, 13000.0, 11000.0, 15000.0),
    }
    query_mock = Mock(side_effect=lambda _db, q, _at, **_kw: [cells[q.filters[0].val]])
    monkeypatch.setattr(executor, "run_query", query_mock)
    return db, cells, query_mock


def test_two_teams_use_real_gate_and_recorded_populations(demo):
    db, cells, query_mock = demo
    assert warmup.run_warmup(db) == 2
    assert db.client.start_session.call_count == 1
    assert query_mock.call_count == 2
    for doc, entry, (user, department) in zip(db.release_docs, db.audit_docs, warmup.SCENARIOS):
        assert doc["ids"] == sorted(cells[department].ids)
        assert doc["n"] == cells[department].n
        assert doc["user"] == user
        assert doc["team"] == ("sales" if user == "mgr_1" else "hr")
        assert doc["project"] == entry["project"] == "comp-review-2026"
        assert doc["session_id"] == entry["session_id"] == "warmup"
        assert doc["created_at"] == entry["time"] == clock.DEMO_START
        assert entry["status"] == "allowed" and entry["memory_on"] is True
        assert entry["kind"] == "gate_decision" and entry["role"] == "manager"
        assert entry["policy_version"] == doc["policy_version"] == 3
        assert entry["rule"] is None and entry["evidence_text"] is None
        assert "value" not in doc and "value" not in entry
    sessions = [call.kwargs["session"] for call in db.releases.insert_many.call_args_list]
    assert sessions[0] is sessions[1]
    assert all(call.kwargs["session"] is sessions[0]
               for call in db.ledger.insert_one.call_args_list)


def test_duplicate_warmup_does_not_append_observations(demo):
    db, _, query_mock = demo
    warmup.run_warmup(db)
    with pytest.raises(warmup.WarmupError, match="project history"):
        warmup.run_warmup(db)
    assert len(db.release_docs) == len(db.audit_docs) == 2
    assert query_mock.call_count == 2


def test_seeded_history_blocks_hire_and_raise_but_allows_memory_off(demo):
    db, cells, _ = demo
    warmup.run_warmup(db)
    for doc, entry, (_, department) in zip(db.release_docs, db.audit_docs, warmup.SCENARIOS):
        old = Release(ids=set(doc["ids"]), target=doc["target"], metric=doc["metric"],
                      created_at=doc["created_at"], cell_key=doc["cell_key"],
                      label=doc["label"], user=doc["user"])
        current = deepcopy(cells[department])
        updated_at = {}
        if department == "Sales":
            current.ids.add(473)
            current.n += 1
        else:
            updated_at = {1408: {"MonthlyIncome": clock.DEMO_START + timedelta(days=2)}}
        query = StructuredQuery(**entry["query"])
        decision = gate.check(query, "manager", [current], [old], updated_at, POLICY)
        assert (decision.status, decision.rule) == ("blocked", "L4.pairwise")
        assert gate.check(query, "manager", [current], [], {}, POLICY).status == "allowed"


@pytest.mark.parametrize("change, message", [
    ({"offset": 3}, "Monday"), ({"dirty": True}, "unmodified employees"),
])
def test_wrong_start_state_is_rejected_before_query(demo, change, message):
    db, _, query_mock = demo
    for key, value in change.items():
        setattr(db, key, value)
    with pytest.raises(warmup.WarmupError, match=message):
        warmup.run_warmup(db)
    query_mock.assert_not_called()
    assert not db.release_docs and not db.audit_docs


def test_missing_data_is_reported(demo):
    db, _, query_mock = demo
    db.empty = True
    with pytest.raises(LookupError, match="employees is empty"):
        warmup.run_warmup(db)
    query_mock.assert_not_called()


def test_both_gate_decisions_are_checked_before_writing(demo):
    db, cells, _ = demo
    cells["Human Resources"] = Cell({}, {10, 11}, 2, 13000.0, 11000.0, 15000.0)
    with pytest.raises(warmup.WarmupError, match="L2.min_n"):
        warmup.run_warmup(db)
    db.releases.insert_many.assert_not_called()
    db.ledger.insert_one.assert_not_called()


def test_precheck_refusal_never_runs_executor(demo, monkeypatch):
    db, _, query_mock = demo
    monkeypatch.setattr(gate, "precheck", lambda *_a: Decision("blocked", rule="L1.role"))
    with pytest.raises(warmup.WarmupError, match="L1.role"):
        warmup.run_warmup(db)
    query_mock.assert_not_called()
    assert not db.release_docs


def test_empty_population_is_not_a_successful_warmup(demo):
    db, _, query_mock = demo
    query_mock.side_effect = None
    query_mock.return_value = []
    with pytest.raises(warmup.WarmupError, match="empty population"):
        warmup.run_warmup(db)
    assert not db.release_docs


def test_second_audit_failure_rolls_back_both_teams(demo):
    db, _, _ = demo

    def append(doc, **_kwargs):
        if doc["team"] == "hr":
            raise RuntimeError("audit unavailable")
        db.audit_docs.append(deepcopy(doc))

    db.ledger.insert_one.side_effect = append
    with pytest.raises(RuntimeError, match="audit unavailable"):
        warmup.run_warmup(db)
    assert db.releases.insert_many.call_count == 2
    assert not db.release_docs and not db.audit_docs


def test_existing_reset_transaction_is_reused(demo):
    db, _, _ = demo
    with Session(db) as session:
        assert session.with_transaction(lambda s: warmup.run_warmup(db, session=s)) == 2
    db.client.start_session.assert_not_called()


def test_unstarted_session_is_rejected(demo):
    db, _, _ = demo
    with pytest.raises(ValueError, match="active transaction"):
        warmup.run_warmup(db, session=Session(db))
    assert not db.release_docs


def test_release_writer_rejects_unstarted_shared_session(demo):
    db, _, _ = demo
    with pytest.raises(ValueError, match="active transaction"):
        releases.record_then_release(db, None, None, None, "p", "s", clock.DEMO_START,
                                     {}, {}, session=Session(db))


def test_transaction_schema_bypasses_and_does_not_poison_cache(monkeypatch):
    executor.clear_cache()
    db = SimpleNamespace(employees=SimpleNamespace(find_one=Mock(return_value={"Salary": int(1)})))
    assert executor.field_types(db) == {"Salary": int}
    db.employees.find_one.return_value = {"Salary": 1.5}
    assert executor.field_types(db, session=object()) == {"Salary": float}
    assert executor.field_types(db) == {"Salary": int}
    executor.clear_cache()
    assert executor.field_types(db) == {"Salary": float}
    executor.clear_cache()


@pytest.mark.parametrize("value", [-(2 ** 63), 2 ** 63 - 1])
@pytest.mark.parametrize("session", [None, object()], ids=["cached", "transaction"])
def test_bson_int64_remains_numeric_after_round_trip(value, session, monkeypatch):
    stored = BSON.encode({"MonthlyIncome": value, "flag": True}).decode()
    assert type(stored["MonthlyIncome"]) is Int64
    monkeypatch.setattr(executor, "_field_types_cache", None)
    db = SimpleNamespace(employees=SimpleNamespace(find_one=Mock(return_value=stored)))
    types = executor.field_types(db, session=session)
    assert types == {"MonthlyIncome": int, "flag": bool}
    query = StructuredQuery(metric="avg", target="MonthlyIncome")
    pipeline = executor.build_pipeline(query, clock.DEMO_START, types)
    assert pipeline[1]["$group"]["value"] == {"$avg": "$MonthlyIncome"}
    with pytest.raises(executor.QueryError, match="not numeric"):
        executor.build_pipeline(StructuredQuery(metric="avg", target="flag"),
                                clock.DEMO_START, types)
