"""Simulated clock so the demo can jump from Monday to Thursday, or past an expiry.

The clock does NOT follow real time: it starts at DEMO_START (the warmup "Monday")
and only moves when advance() is called. now() = DEMO_START + offset.
/admin/reset sets the offset back to 0. Every timestamp the app writes
(releases.created_at, employees.updated_at, ledger.time) comes from now(), so they
are all comparable.

Demo timeline (matches tests/cases/gate_cases.json):
  Mon 2026-11-09  warmup queries ("last week")
  Wed 2026-11-11  raise via /admin/simulate-update
  Thu 2026-11-12  new hire #473 starts (scripts/load_data.py); live attack queries
"""

from datetime import datetime, timedelta, timezone

DEMO_START = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)      # Monday 09:00 UTC
NEW_HIRE_START = DEMO_START + timedelta(days=3)                    # Thursday 09:00 UTC


def now() -> datetime:
    """DEMO_START plus the current demo offset."""
    raise NotImplementedError


def advance(days: float) -> datetime:
    """Move the demo clock forward; return the new 'now'."""
    raise NotImplementedError
