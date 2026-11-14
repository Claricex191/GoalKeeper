"""Policy upgrades preserve retired legacy audit records without serving their grants.

The fake store checks the migration-critical required fields, enums and thresholds
from the actual schemas. Full BSON validation remains covered by Atlas tests.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.policy import store
from app.policy.defaults import POLICY

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def legacy_policy(schema_version=None, status="active"):
    doc = {"_id": "ibm_hr_v1", "dataset": "ibm_hr", "version": 1,
           "status": status, "drafted_by": "manual", "approved_by": "previous-reviewer",
           "k_threshold": 5, "sensitive_targets": ["MonthlyIncome"],
           "roles": {"manager": {"targets": ["MonthlyIncome"], "metrics": ["avg"]}}}
    if schema_version is not None:
        doc["schema_version"] = schema_version
    return doc


class Collection:
    def __init__(self, docs=(), validate=None):
        # These documents predate installation of the current validator.
        self.docs = deepcopy(list(docs))
        self.validate = validate
        self.writes = []

    def find_one(self, filt, sort=None):
        found = [doc for doc in self.docs if all(doc.get(k) == v for k, v in filt.items())]
        if sort:
            key, direction = sort[0]
            found.sort(key=lambda doc: doc[key], reverse=direction < 0)
        return deepcopy(found[0]) if found else None

    def insert_one(self, doc, session=None):
        if self.validate:
            self.validate(doc)
        self.docs.append(deepcopy(doc))
        self.writes.append(("insert", doc["_id"] if "_id" in doc else None))

    def update_one(self, filt, update, session=None):
        for index, old in enumerate(self.docs):
            if all(old.get(k) == v for k, v in filt.items()):
                doc = {**old, **deepcopy(update["$set"])}
                if self.validate:
                    self.validate(doc)
                self.docs[index] = doc
                self.writes.append(("update", doc["_id"], doc["status"]))
                return
        raise AssertionError("update did not match a policy")


@pytest.fixture
def db_factory(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=100")
    from app.db import POLICIES_VALIDATOR

    def validate(doc):
        for branch in POLICIES_VALIDATOR["$jsonSchema"]["anyOf"]:
            if not set(branch["required"]).issubset(doc):
                continue
            props = branch["properties"]
            if any(key in doc and doc[key] not in spec["enum"]
                   for key, spec in props.items() if "enum" in spec):
                continue
            if doc["k_threshold"] < props["k_threshold"]["minimum"]:
                continue
            if doc["status"] == "active" and not doc.get("approved_by"):
                continue
            return
        raise ValueError("policy rejected by migration validator")

    def make(*docs):
        db = SimpleNamespace(policies=Collection(docs, validate), ledger=Collection())

        class Session:
            def with_transaction(self, callback):
                policies, ledger = deepcopy(db.policies.docs), deepcopy(db.ledger.docs)
                try:
                    return callback(self)
                except Exception:
                    db.policies.docs, db.ledger.docs = policies, ledger
                    raise

        @contextmanager
        def start_session():
            yield Session()

        db.client = SimpleNamespace(start_session=start_session)
        return db

    monkeypatch.setattr(store.clock, "now", lambda: NOW)
    store.clear_cache()
    yield make
    store.clear_cache()


@pytest.mark.parametrize("schema_version", [None, 1])
def test_v2_activation_retires_legacy_policy_under_current_validator(db_factory, schema_version):
    db = db_factory(legacy_policy(schema_version))
    version = store.create_draft(db, "ibm_hr", POLICY, drafted_by="manual")
    assert version == 2
    store.activate(db, "ibm_hr", version, approved_by="reviewer")
    old = db.policies.find_one({"version": 1})
    assert old["status"] == "retired"
    assert old["retired_at"] == NOW
    assert "field_categories" not in old  # Preserve the original audit record.
    assert store.load_active(db, "ibm_hr")["schema_version"] == 2
    assert db.policies.writes[-2:] == [
        ("update", "ibm_hr_v1", "retired"), ("update", "ibm_hr_v2", "active")]
    assert len(db.ledger.docs) == 1
    assert (db.ledger.docs[0]["from_version"], db.ledger.docs[0]["to_version"]) == (1, 2)
    with pytest.raises(ValueError, match="schema_version must be 2"):
        store.activate(db, "ibm_hr", 1, approved_by="reviewer")
    assert store.load_active(db, "ibm_hr")["version"] == 2


@pytest.mark.parametrize("schema_version", [None, 1])
@pytest.mark.parametrize("status", ["draft", "active", "retired"])
def test_legacy_policy_can_never_be_activated(db_factory, schema_version, status):
    db = db_factory(legacy_policy(schema_version, status))
    with pytest.raises(ValueError, match="schema_version must be 2"):
        store.activate(db, "ibm_hr", 1, approved_by="reviewer")
    assert not db.policies.writes
    assert not db.ledger.docs


@pytest.mark.parametrize("schema_version", [None, 1])
def test_legacy_policy_cannot_be_created_or_read_as_active(db_factory, schema_version):
    doc = legacy_policy(schema_version)
    db = db_factory(doc)
    with pytest.raises(ValueError, match="schema_version must be 2"):
        store.create_draft(db, "ibm_hr", doc, drafted_by="manual")
    with pytest.raises(ValueError, match="schema_version must be 2"):
        store.load_active(db, "ibm_hr")
    # A stale cache must not bypass the version check either.
    store._active_cache["ibm_hr"] = doc
    with pytest.raises(ValueError, match="schema_version must be 2"):
        store.load_active(db, "ibm_hr")
    assert not db.policies.writes


def test_only_legacy_retired_documents_use_the_audit_exception(db_factory):
    db = db_factory()
    db.policies.insert_one(legacy_policy(status="retired"))
    db.policies.insert_one(legacy_policy(schema_version=1, status="retired"))
    for schema_version, status in [(None, "draft"), (None, "active"), (1, "active"),
                                   (2, "retired"), (3, "retired")]:
        with pytest.raises(ValueError, match="migration validator"):
            db.policies.insert_one(legacy_policy(schema_version, status))


def test_retired_v2_policy_remains_available_for_rollback(db_factory):
    db = db_factory()
    first = store.create_draft(db, "ibm_hr", POLICY, drafted_by="manual")
    store.activate(db, "ibm_hr", first, approved_by="reviewer")
    second = store.create_draft(db, "ibm_hr", POLICY, drafted_by="manual")
    store.activate(db, "ibm_hr", second, approved_by="reviewer")
    store.activate(db, "ibm_hr", first, approved_by="reviewer")
    assert store.load_active(db, "ibm_hr")["version"] == first
    assert db.policies.find_one({"version": second})["status"] == "retired"
