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


def get_db():
    """Return the pymongo Database for settings.mongodb_db."""
    raise NotImplementedError


def ensure_indexes(db) -> None:
    """Create TTL index on releases.expires_at, lookup indexes, and $jsonSchema validators."""
    raise NotImplementedError
