"""Atlas client, collections and indexes.

Full field lists, $jsonSchema validators and indexes: docs/collections.md

Collections
- employees   IBM HR records (+ `active`, `AgeBand`, `start_date`, `updated_at`)
- policies    roles, sensitive fields, thresholds            (schema validation)
- releases    disclosure ledger = security memory             (schema validation, TTL on expires_at)
- precedents  reviewer rulings + embedding = semantic memory  (Vector Search)
- ledger      append-only audit log                           (schema validation)
- approvals   review requests + kill switch                   (change streams)

Vector Search index (create in Atlas UI on `precedents`):
{
  "fields": [
    {"type": "vector", "path": "embedding", "numDimensions": <voyage dims>, "similarity": "cosine"},
    {"type": "filter", "path": "project"},
    {"type": "filter", "path": "overturned"}
  ]
}
"""

from pymongo import ASCENDING, MongoClient
from pymongo.database import Database

from app.config import settings


_client: MongoClient | None = None


def get_db() -> Database:
    """Return the pymongo Database for settings.mongodb_db (one shared client).
    tz_aware=True: dates come back as UTC-aware datetimes, comparable with clock.now()."""
    global _client
    if _client is None:
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
    Done: releases. TODO: ledger and policies validators (docs/collections.md)."""
    _ensure_validator(db, "releases", RELEASES_VALIDATOR)
    db.releases.create_index([("project", ASCENDING), ("target", ASCENDING), ("user", ASCENDING)])
    db.releases.create_index([("project", ASCENDING), ("target", ASCENDING), ("team", ASCENDING)])
    # Physical cleanup in REAL time; logical expiry is checked against clock.now() in
    # memory.releases.load_history (the demo clock is simulated).
    db.releases.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
