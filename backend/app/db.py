"""Atlas client, collections and indexes.

Full field lists, $jsonSchema validators and indexes: docs/collections.md

Collections
- employees   IBM HR records (+ `active`, `AgeBand`, `start_date`, `updated_at`)
- policies    roles, sensitive fields, thresholds            (schema validation)
- releases    disclosure ledger = security memory             (schema validation, TTL on expires_at)
- precedents  reviewer rulings + embedding = semantic memory  (Vector Search)
- ledger      append-only audit log                           (schema validation)
- approvals   review requests + kill switch                   (change streams)

Vector Search index `precedents_vec` on `precedents`: created in code by
ensure_vector_index() (definition in PRECEDENTS_VECTOR_INDEX), not by hand in the UI.
"""

import logging
import time

from pymongo import ASCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from app.config import settings


_client: MongoClient | None = None


def get_db() -> Database:
    """Return the pymongo Database for settings.mongodb_db (one shared client).
    tz_aware=True: dates come back as UTC-aware datetimes, comparable with clock.now()."""
    global _client
    if _client is None:
        if not settings.mongodb_uri:
            raise RuntimeError("MONGODB_URI is not set: add it to backend/.env (see README)")
        _client = MongoClient(settings.mongodb_uri, tz_aware=True)
    return _client[settings.mongodb_db]


RELEASES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["user", "team", "project", "target", "metric", "cell_key", "label",
                     "ids", "n", "policy_version", "created_at", "expires_at"],
        "properties": {
            "user": {"bsonType": "string"},
            "team": {"bsonType": "string"},
            "project": {"bsonType": "string"},
            "session_id": {"bsonType": "string"},
            "target": {"bsonType": "string"},
            "metric": {"enum": ["count", "sum", "avg"]},
            "cell_key": {"bsonType": "object"},
            "label": {"bsonType": "string"},
            "ids": {"bsonType": "array", "items": {"bsonType": "int"}, "minItems": 5},
            "n": {"bsonType": "int", "minimum": 5},
            "policy_version": {"bsonType": "int"},
            "created_at": {"bsonType": "date"},
            "expires_at": {"bsonType": "date"},
        },
        "additionalProperties": True,
    }
}


LEDGER_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["kind", "time", "user"],
        "properties": {
            "kind": {"enum": ["gate_decision", "data_update", "policy_change"]},
            "time": {"bsonType": "date"},
            "user": {"bsonType": "string"},
        },
        # exactly one branch per kind, with that kind's extra required fields
        "oneOf": [
            {"properties": {"kind": {"enum": ["gate_decision"]},
                            "query": {"bsonType": "object"},
                            "status": {"enum": ["allowed", "partial", "blocked", "error"]},
                            "policy_version": {"bsonType": "int"},
                            "memory_on": {"bsonType": "bool"}},
             "required": ["query", "status", "policy_version"]},
            {"properties": {"kind": {"enum": ["data_update"]},
                            "employee_number": {"bsonType": "int"},
                            "field": {"bsonType": "string"}},
             "required": ["employee_number", "field"],
             "not": {"required": ["new_value"]}},       # never log the new value
            {"properties": {"kind": {"enum": ["policy_change"]},
                            "dataset": {"bsonType": "string"},
                            "from_version": {"bsonType": ["int", "null"]},
                            "to_version": {"bsonType": "int"}},
             "required": ["dataset", "from_version", "to_version"]},
        ],
        "additionalProperties": True,
    }
}

POLICIES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["dataset", "version", "status", "drafted_by", "k_threshold",
                     "sensitive_targets", "roles"],
        "properties": {
            "dataset": {"bsonType": "string"},
            "version": {"bsonType": "int"},
            "status": {"enum": ["draft", "active", "retired"]},
            "drafted_by": {"bsonType": "string"},
            "approved_by": {"bsonType": "string"},
            "k_threshold": {"bsonType": "int", "minimum": 2},
            "sensitive_targets": {"bsonType": "array", "items": {"bsonType": "string"}},
            "roles": {"bsonType": "object"},
        },
        # an active policy must name the human who approved it
        "anyOf": [
            {"properties": {"status": {"enum": ["draft", "retired"]}}},
            {"properties": {"status": {"enum": ["active"]}}, "required": ["approved_by"]},
        ],
        "additionalProperties": True,
    }
}


def _ensure_validator(db, name: str, validator: dict) -> None:
    """Create the collection with this validator, or update it if it already exists."""
    if name in db.list_collection_names():
        db.command("collMod", name, validator=validator, validationAction="error",
                   validationLevel="strict")
    else:
        db.create_collection(name, validator=validator, validationAction="error",
                             validationLevel="strict")


def ensure_indexes(db) -> None:
    """Create TTL index on releases.expires_at, lookup indexes, and $jsonSchema validators.
    Idempotent: safe to call on every startup.
    Validators: releases, ledger, policies (docs/collections.md)."""
    _ensure_validator(db, "releases", RELEASES_VALIDATOR)
    _ensure_validator(db, "ledger", LEDGER_VALIDATOR)
    _ensure_validator(db, "policies", POLICIES_VALIDATOR)
    db.releases.create_index([("project", ASCENDING), ("target", ASCENDING), ("user", ASCENDING)])
    db.releases.create_index([("project", ASCENDING), ("target", ASCENDING), ("team", ASCENDING)])
    # Physical cleanup in REAL time; logical expiry is checked against clock.now() in
    # memory.releases.load_history (the demo clock is simulated).
    db.releases.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    ensure_vector_index(db)


log = logging.getLogger(__name__)

PRECEDENTS_VECTOR_INDEX = "precedents_vec"


def precedents_vector_definition() -> dict:
    """$vectorSearch on precedents.embedding (Voyage, settings.voyage_model).
    project / overturned are pre-filters: search only this project's live rulings."""
    return {
        "fields": [
            {"type": "vector", "path": "embedding",
             "numDimensions": settings.voyage_dims, "similarity": "cosine"},
            {"type": "filter", "path": "project"},
            {"type": "filter", "path": "overturned"},
        ]
    }


def ensure_vector_index(db) -> None:
    """Create precedents_vec if missing; update it if numDimensions changed.
    Never blocks startup: a failure is logged, and precedents search degrades to
    "no similar precedents" rather than taking the app down."""
    try:
        if "precedents" not in db.list_collection_names():
            db.create_collection("precedents")   # a search index needs the collection
        definition = precedents_vector_definition()
        existing = {i["name"]: i for i in db.precedents.list_search_indexes()}
        current = existing.get(PRECEDENTS_VECTOR_INDEX)
        if current is None:
            db.precedents.create_search_index(SearchIndexModel(
                name=PRECEDENTS_VECTOR_INDEX, type="vectorSearch", definition=definition))
            log.info("created vector index %s", PRECEDENTS_VECTOR_INDEX)
        elif current.get("latestDefinition") != definition:
            db.precedents.update_search_index(PRECEDENTS_VECTOR_INDEX, definition)
            log.info("updated vector index %s", PRECEDENTS_VECTOR_INDEX)
    except OperationFailure as exc:
        log.warning("vector index %s not ensured: %s", PRECEDENTS_VECTOR_INDEX, exc)


def wait_vector_index_ready(db, timeout_s: float = 120) -> bool:
    """Poll until precedents_vec is queryable (a new index takes ~1 min). For scripts."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for i in db.precedents.list_search_indexes(PRECEDENTS_VECTOR_INDEX):
            if i.get("queryable"):
                return True
        time.sleep(3)
    return False
