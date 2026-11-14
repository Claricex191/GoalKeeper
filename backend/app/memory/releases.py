"""Disclosure ledger: the security memory.

Stores what each user has been told (population ids per released cell), never the
conversation and never the released values. Must be complete and lossless.
Governance rules, all read from the ACTIVE POLICY (policy["memory"]):
- scope: user / team / org – who shares a memory. The agent can ask "does this
  conflict?" (via the gate) but never reads another user's released values.
- retention: expires_at = created_at + retention_by_target[target] (or retention_days).
  Long for data that stays linkable for long (pay); expiry is checked at query time
  against the simulated clock; the TTL index only cleans up physically, in real time.
- users cannot delete their own releases.

Cross-session safety comes from here, not from chat history: a release is keyed by
user / team, never by session, so a new session sees everything already disclosed.
"""

from datetime import datetime, timedelta

from app.memory import ledger
from app.schemas import Decision, Identity, Release, StructuredQuery

METRIC_WORDS = {"avg": "average", "sum": "total", "count": "count"}


# --- pure helpers (no database) -----------------------------------------------------
def release_targets(query: StructuredQuery, policy: dict) -> list[str]:
    """Which sensitive fields this answer discloses something about.
    - a target (sum / avg)                       → [target]
    - a pure count filtered on sensitive fields  → those fields (the population itself
      reveals them, e.g. "Attrition = Yes" tells who left)
    - otherwise                                  → [] (nothing to remember)"""
    if query.target:
        return [query.target]
    sensitive = set(policy["sensitive_targets"])
    found = []
    for f in query.filters:
        if f.col in sensitive and f.col not in found:
            found.append(f.col)
    return found


def retention_days(policy: dict, target: str) -> int:
    mem = policy["memory"]
    return int(mem.get("retention_by_target", {}).get(target, mem["retention_days"]))


def make_label(query: StructuredQuery, cell_key: dict) -> str:
    """Deterministic description for evidence text. Contains the question, never values.
    e.g. "Department=Sales, JobRole=Manager | Gender=Female | average MonthlyIncome" """
    parts = []
    if query.filters:
        parts.append(", ".join(
            f"{f.col} in {f.val}" if f.op == "in" else f"{f.col}{f.op}{f.val}"
            for f in query.filters))
    if cell_key:
        parts.append(", ".join(f"{k}={v}" for k, v in cell_key.items()))
    what = METRIC_WORDS[query.metric]
    parts.append(f"{what} {query.target}" if query.target else what)
    return " | ".join(parts) if parts else what


def _scope_filter(who: Identity, policy: dict) -> dict:
    scope = policy["memory"]["scope"]
    if scope == "user":
        return {"user": who.user}
    if scope == "team":
        return {"team": who.team}
    if scope == "org":
        return {}
    raise ValueError(f"unknown memory scope {scope!r}")


# --- database -----------------------------------------------------------------------
def load_history(db, who: Identity, project: str, targets: list[str], now: datetime,
                 policy: dict) -> list[Release]:
    """ALL unexpired releases for these targets within the policy's scope:
    user → {user: who.user}, team → {team: who.team}, org → no user/team filter.
    Exact query, no limit: missing one release could miss the one that completes
    the puzzle."""
    if not targets:
        return []
    filt = {"project": project, "target": {"$in": targets}, "expires_at": {"$gt": now},
            **_scope_filter(who, policy)}
    return [
        Release(ids=set(d["ids"]), target=d["target"], metric=d["metric"],
                created_at=d["created_at"], cell_key=d.get("cell_key", {}),
                label=d.get("label", ""), user=d.get("user"))
        for d in db.releases.find(filt).sort("created_at", 1)
    ]


def record_then_release(db, decision: Decision, query: StructuredQuery,
                        who: Identity, project: str, session_id: str, now: datetime,
                        policy: dict, ledger_entry: dict, *, session=None) -> int:
    """Write one release per (returned cell × release target) AND the ledger entry in
    one transaction, stamped with who.user and who.team. Returns the number of releases.

    Only after this returns may the caller return the cells (fail closed): any error
    (including the releases validator rejecting n < 5) raises and rolls back both
    the releases and the ledger entry. Suppressed cells are never recorded.
    Each record stores `cell_key` and `label` (see make_label) for evidence.render.
    If `session` is supplied, it must already be in a transaction; the caller must
    commit that transaction before returning any cells. This lets reset/warmup
    atomically seed both teams alongside the restored data."""
    if session is not None and not session.in_transaction:
        raise ValueError("record_then_release requires an active transaction")
    docs = []
    for target in release_targets(query, policy):
        expires = now + timedelta(days=retention_days(policy, target))
        for cell in decision.cells:
            docs.append({
                "user": who.user, "team": who.team, "project": project,
                "session_id": session_id,
                "target": target, "metric": query.metric,
                "cell_key": dict(cell.key), "label": make_label(query, cell.key),
                "ids": sorted(int(i) for i in cell.ids), "n": len(cell.ids),
                "policy_version": int(policy["version"]),
                "created_at": now, "expires_at": expires,
            })

    def txn(session):
        if docs:
            db.releases.insert_many(docs, session=session)
        ledger.append(db, ledger_entry, session=session)

    if session is not None:
        txn(session)
    else:
        with db.client.start_session() as own_session:
            own_session.with_transaction(txn)
    return len(docs)
