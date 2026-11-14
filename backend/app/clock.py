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

DEMO_START = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)      # Monday 09:00 UTC
NEW_HIRE_START = DEMO_START + timedelta(days=3)                    # Thursday 09:00 UTC

_CLOCK_ID = "clock"


def _meta():
    # Imported lazily so modules that only need the constants (e.g. load_data --dry-run)
    # do not require a database connection or a .env file.
    from app.db import get_db
    return get_db().meta


def offset_days() -> float:
    doc = _meta().find_one({"_id": _CLOCK_ID})
    return float(doc["offset_days"]) if doc else 0.0


def now() -> datetime:
    """DEMO_START plus the current demo offset."""
    return DEMO_START + timedelta(days=offset_days())


def advance(days: float) -> datetime:
    """Move the demo clock forward; return the new 'now'.
    Only forward: going back would make new records look older than existing ones."""
    if days <= 0:
        raise ValueError("the demo clock only moves forward; use reset() to start over")
    _meta().update_one({"_id": _CLOCK_ID}, {"$inc": {"offset_days": float(days)}}, upsert=True)
    return now()


def reset() -> datetime:
    """Back to DEMO_START (used by /admin/reset before re-running the warmup)."""
    _meta().update_one({"_id": _CLOCK_ID}, {"$set": {"offset_days": 0.0}}, upsert=True)
    return now()
