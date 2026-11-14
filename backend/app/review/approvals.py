"""Review requests (human in the loop).

L5.probing creates one when a user keeps asking about the same sensitive field after a
block (see agent.tools._probing). While a request is pending, that user's queries on
that field are refused. A decision (approve or reject) unlocks the field and restarts
the count. Documents never contain released values or employee ids.

{"_id": ObjectId, "kind": "review", "user", "team", "project", "target",
 "query": {...last attempt...}, "reason", "status": "pending|approved|rejected",
 "transform": null, "reviewer": null, "created_at", "decided_at": null}
"""

from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId

STATUSES = ("pending", "approved", "rejected")


def create(db, user: str, team: str, project: str, target: str, query: dict,
           reason: str, now: datetime) -> str:
    """Insert a pending review; return its id."""
    res = db.approvals.insert_one({
        "kind": "review", "user": user, "team": team, "project": project,
        "target": target, "query": query, "reason": reason, "status": "pending",
        "transform": None, "reviewer": None, "created_at": now, "decided_at": None,
    })
    return str(res.inserted_id)


def pending_for(db, user: str, project: str, target: str) -> dict | None:
    return db.approvals.find_one({"kind": "review", "status": "pending", "user": user,
                                  "project": project, "target": target})


def list_pending(db, project: str | None = None) -> list[dict]:
    """For GET /review/pending (frontend/API.md)."""
    filt = {"kind": "review", "status": "pending"}
    if project:
        filt["project"] = project
    return [{"id": str(d["_id"]), "user": d["user"], "target": d["target"],
             "query": d["query"], "reason": d["reason"], "created_at": d["created_at"]}
            for d in db.approvals.find(filt).sort("created_at", 1)]


def decide(db, approval_id: str, approve: bool, transform: str | None, reviewer: str,
           now: datetime) -> bool:
    """Approve or reject a pending review. False if not found or already decided."""
    try:
        oid = ObjectId(approval_id)
    except (InvalidId, TypeError):
        return False
    res = db.approvals.update_one(
        {"_id": oid, "kind": "review", "status": "pending"},
        {"$set": {"status": "approved" if approve else "rejected", "transform": transform,
                  "reviewer": reviewer, "decided_at": now,
                  # the probing count restarts after this point. An ObjectId, not a
                  # time: the demo clock does not move within a day, ids do.
                  "reset_after": ObjectId()}})
    return res.modified_count == 1


def reset_point(db, user: str, project: str, target: str) -> ObjectId | None:
    """Ledger entries after this id count toward L5; None = count from the start."""
    d = db.approvals.find_one({"kind": "review", "user": user, "project": project,
                               "target": target, "reset_after": {"$exists": True}},
                              sort=[("reset_after", -1)])
    return d["reset_after"] if d else None
