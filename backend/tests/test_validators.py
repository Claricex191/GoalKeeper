"""$jsonSchema validators on ledger and policies, against Atlas. Skipped without Atlas.

Only invalid documents are inserted (and rejected), so nothing is left behind; the
one valid-document check runs `validate` on the existing collection data instead.
"""
from datetime import datetime, timezone

import pytest
from pymongo.errors import WriteError

NOW = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)


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


@pytest.mark.parametrize("doc", [
    {"kind": "nonsense", "time": NOW, "user": "x"},                      # unknown kind
    {"kind": "gate_decision", "time": NOW, "user": "x"},                 # missing query/status
    {"kind": "gate_decision", "time": NOW, "user": "x", "query": {},
     "status": "maybe", "policy_version": 2},                            # bad status
    {"kind": "data_update", "time": NOW, "user": "x",
     "employee_number": 1408, "field": "MonthlyIncome", "new_value": 18500},  # leaks value
    {"kind": "policy_change", "time": NOW, "user": "x", "dataset": "d"},     # no versions
    {"kind": "data_update", "user": "x", "employee_number": 1, "field": "f"},  # no time
])
def test_ledger_rejects(db, doc):
    with pytest.raises(WriteError):
        db.ledger.insert_one(doc)


@pytest.mark.parametrize("doc", [
    {"dataset": "t", "version": 99, "status": "active", "drafted_by": "x",
     "k_threshold": 5, "sensitive_targets": [], "roles": {}},            # active, no approver
    {"dataset": "t", "version": 99, "status": "draft", "drafted_by": "x",
     "k_threshold": 1, "sensitive_targets": [], "roles": {}},            # k < 2
    {"dataset": "t", "version": 99, "status": "live", "drafted_by": "x",
     "k_threshold": 5, "sensitive_targets": [], "roles": {}},            # bad status
])
def test_policies_rejects(db, doc):
    with pytest.raises(WriteError):
        db.policies.insert_one(doc)


@pytest.mark.parametrize("name", ["ledger", "policies"])
def test_existing_documents_are_valid(db, name):
    res = db.command("validate", name)
    assert res.get("nInvalidDocuments", 0) == 0, res.get("warnings")
