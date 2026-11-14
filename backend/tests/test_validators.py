"""$jsonSchema validators on ledger and policies, against Atlas. Skipped without Atlas.

Only invalid documents are inserted (and rejected), so nothing is left behind; the
one valid-document check runs `validate` on the existing collection data instead.
"""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from pymongo.errors import WriteError

from app.policy.defaults import POLICY

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


def policy_doc(**changes):
    return {**deepcopy(POLICY), "dataset": "t", "version": 99, "status": "draft",
            "drafted_by": "x", **changes}


@pytest.mark.parametrize("doc", [
    policy_doc(status="active"),                     # active, no approver
    policy_doc(k_threshold=4),                       # the mean floor cannot be lowered
    policy_doc(k_threshold=1),
    policy_doc(status="live"),
    policy_doc(schema_version=1),
    policy_doc(roles={"hr": {"targets": ["*"], "metrics": ["avg"]}}),
    policy_doc(field_categories={"StockOptionLevel": "unknown"}),
    policy_doc(field_metrics={"Age": ["execute_python"]}),
    policy_doc(roles={**deepcopy(POLICY["roles"]), "employee": {
        "scope": "none", "grants": {"other_basic": ["everything"]}}}),
])
def test_policies_rejects(db, doc):
    with pytest.raises(WriteError):
        db.policies.insert_one(doc)


def test_policy_validator_requires_the_new_authorization_contract(monkeypatch):
    # Inspect the validator without connecting to a database.
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    from app.db import (LEGACY_RETIRED_SCHEMA, POLICIES_VALIDATOR, POLICY_V2_SCHEMA,
                        RELEASES_VALIDATOR)
    assert POLICIES_VALIDATOR["$jsonSchema"]["anyOf"] == [POLICY_V2_SCHEMA, LEGACY_RETIRED_SCHEMA]
    schema = POLICY_V2_SCHEMA
    assert schema["properties"]["k_threshold"]["minimum"] == 5
    assert {"schema_version", "field_categories", "field_metrics", "roles"} <= set(schema["required"])
    roles = schema["properties"]["roles"]
    assert set(roles["required"]) == set(POLICY["roles"])
    assert roles["additionalProperties"] is False
    assert LEGACY_RETIRED_SCHEMA["properties"]["status"]["enum"] == ["retired"]
    assert LEGACY_RETIRED_SCHEMA["properties"]["schema_version"]["enum"] == [1]
    assert "schema_version" not in LEGACY_RETIRED_SCHEMA["required"]
    assert "median" in RELEASES_VALIDATOR["$jsonSchema"]["properties"]["metric"]["enum"]
    assert RELEASES_VALIDATOR["$jsonSchema"]["properties"]["n"]["minimum"] == 5


@pytest.mark.parametrize("name", ["ledger", "policies"])
def test_existing_documents_are_valid(db, name):
    res = db.command("validate", name)
    assert res.get("nInvalidDocuments", 0) == 0, res.get("warnings")
