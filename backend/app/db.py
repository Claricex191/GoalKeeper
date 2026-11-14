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
    """Return the pymongo Database for settings.mongodb_db (one shared client)."""
    global _client
    if _client is None:
        _client = MongoClient(settings.mongodb_uri)
    return _client[settings.mongodb_db]


def ensure_indexes(db) -> None:
    """Create TTL index on releases.expires_at, lookup indexes, and $jsonSchema validators."""
    raise NotImplementedError
