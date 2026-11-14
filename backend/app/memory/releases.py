"""Disclosure ledger: the security memory.

Stores what each user has been told (population ids per released cell), never the
conversation. Must be complete and lossless. Governance rules:
- scope: user / team / org (settings.memory_scope) – the agent can ask "does this conflict?"
  but never read another user's released values
- retention: expires_at tied to the data's validity (TTL index), not to convenience
- users cannot delete their own releases
"""

from datetime import datetime, timedelta

from app.config import settings
from app.schemas import Decision, Identity, Release, StructuredQuery


def load_history(db, who: Identity, project: str, target: str, now: datetime) -> list[Release]:
    """ALL unexpired releases for this target within the configured scope:
    user → {user: who.user}, team → {team: who.team}, org → no user/team filter."""
    raise NotImplementedError


def record_then_release(db, decision: Decision, query: StructuredQuery,
                        who: Identity, project: str, session_id: str, now: datetime) -> None:
    """Write one release per returned cell inside a transaction, stamped with
    who.user and who.team.
    Only after the write succeeds may the caller return the cells (fail closed).
    Each record also stores `cell_key` and `label`: a deterministic description built
    from the query's filters, the cell key, metric and target (never by the LLM, never
    containing values), e.g. "Sales / Manager, average MonthlyIncome". evidence.render
    uses it to say which earlier answer the new one conflicts with."""
    raise NotImplementedError
