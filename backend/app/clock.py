"""Simulated clock so the demo can jump from Monday to Thursday, or past an expiry.

The clock does NOT follow real time: it starts at DEMO_START (the warmup "Monday")
and only moves when advance() is called. now() = DEMO_START + offset.
/admin/reset sets the offset back to 0. Every timestamp the app writes
(releases.created_at, employees.updated_at, ledger.time) comes from now(), so they
are all comparable.

The offset lives in MongoDB (collection `meta`, document {_id: "clock", offset_days}),
not in process memory, because the API server and scripts/warmup.py are separate
processes and must agree on "now".

Demo timeline (matches tests/cases/gate_cases.json):
  Mon 2026-11-09  warmup queries ("last week")
  Wed 2026-11-11  raise via /admin/simulate-update
  Thu 2026-11-12  new hire #473 starts (scripts/load_data.py); live attack queries
"""

from datetime import datetime, timedelta, timezone
from math import isfinite

from pymongo import ReturnDocument

DEMO_START = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)      # Monday 09:00 UTC
NEW_HIRE_START = DEMO_START + timedelta(days=3)                    # Thursday 09:00 UTC

_CLOCK_ID = "clock"
# Leave a full day's margin for floating point rounding near datetime.max.
_MAX_OFFSET_DAYS = (datetime.max.replace(tzinfo=timezone.utc) - DEMO_START).days - 1
_MIN_ADVANCE_DAYS = 1 / 86_400_000  # BSON timestamps have millisecond precision.


def _meta(db=None):
    # Imported lazily so modules that only need the constants (e.g. load_data --dry-run)
    # do not require a database connection or a .env file.
    if db is None:
        from app.db import get_db
        db = get_db()
    return db.meta


def offset_days(*, db=None, session=None) -> float:
    doc = _meta(db).find_one({"_id": _CLOCK_ID}, session=session)
    return float(doc["offset_days"]) if doc else 0.0


def now(*, db=None, session=None) -> datetime:
    """DEMO_START plus the current demo offset."""
    return DEMO_START + timedelta(days=offset_days(db=db, session=session))


def advance(days: float, *, db=None, session=None) -> datetime:
    """Move the demo clock forward; return the new 'now'.
    Only forward: going back would make new records look older than existing ones."""
    days = float(days)
    if not isfinite(days) or days < _MIN_ADVANCE_DAYS or days > _MAX_OFFSET_DAYS:
        raise ValueError("days must be finite, advance at least one millisecond, "
                         "and remain within the supported date range")
    meta = _meta(db)
    meta.update_one({"_id": _CLOCK_ID}, {"$setOnInsert": {"offset_days": 0.0}},
                    upsert=True, session=session)
    # Check the cumulative bound in the write itself, so concurrent advances cannot
    # store an overflowing offset. Return this operation's value, not a later read.
    doc = meta.find_one_and_update(
        {"_id": _CLOCK_ID,
         "offset_days": {"$gte": 0, "$lte": _MAX_OFFSET_DAYS - days}},
        {"$inc": {"offset_days": days}}, return_document=ReturnDocument.AFTER,
        session=session,
    )
    if doc is None:
        raise ValueError("the demo clock would exceed the supported date range; reset first")
    return DEMO_START + timedelta(days=doc["offset_days"])


def reset(*, db=None, session=None) -> datetime:
    """Back to DEMO_START (used by /admin/reset before re-running the warmup)."""
    _meta(db).update_one({"_id": _CLOCK_ID}, {"$set": {"offset_days": 0.0}},
                         upsert=True, session=session)
    return DEMO_START
