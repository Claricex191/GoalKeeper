"""Precedents: the semantic memory (Atlas Vector Search).

Every refused question is remembered as a short description of the QUESTION (never a
value, never an id), embedded with Voyage. When a later rewrite of it is answered, the
precedent records which transform worked. A new refusal searches for similar earlier
ones, so the agent can try the rewrite that worked before first.

Advisory only: a precedent can never make the gate release anything. Every rewrite is
checked again by the gate.

{"project", "overturned": false, "user", "team", "target", "label", "rule",
 "allowed_transforms", "transform": null | "T3_widen_scope" (what worked),
 "created_at", "resolved_at", "embedding": [1024 floats]}

Index: precedents_vec (app.db.ensure_vector_index), filters project and overturned.
Voyage free tier is rate limited (about 3 requests / minute): identical questions are
stored once, embeddings are cached in-process, and failures degrade to "no hints".
"""

import logging
from datetime import datetime

import voyageai

from app.config import settings
from app.db import PRECEDENTS_VECTOR_INDEX
from app.schemas import Decision, Identity, StructuredQuery

log = logging.getLogger(__name__)

METRIC_WORDS = {"avg": "average", "sum": "total", "count": "head count",
                "max": "maximum", "min": "minimum"}
# Atlas cosine score = (1 + cos) / 2. Measured with voyage-3.5-lite: same question with
# one extra filter ≈ 0.87, unrelated question ≈ 0.69.
MIN_SCORE = 0.8

_client: voyageai.Client | None = None
_cache: dict[tuple[str, str], list[float]] = {}


def describe(query: StructuredQuery) -> str:
    """Plain description of the question, e.g.
    "average MonthlyIncome where Department = Sales and JobRole = Manager".
    Used both as the stored label and as the text that is embedded."""
    what = METRIC_WORDS.get(query.metric, query.metric)
    text = f"{what} {query.target}" if query.target else what
    if query.filters:
        text += " where " + " and ".join(
            f"{f.col} in {list(f.val)}" if f.op == "in" else f"{f.col} {f.op} {f.val}"
            for f in query.filters)
    if query.group_by:
        text += " by " + ", ".join(query.group_by)
    return text


def embed(text: str, input_type: str = "document") -> list[float]:
    """Voyage embedding (settings.voyage_model); input_type "document" to store,
    "query" to search."""
    global _client
    key = (text, input_type)
    if key not in _cache:
        if _client is None:
            if not settings.voyage_api_key:
                raise RuntimeError("VOYAGE_API_KEY is not set")
            _client = voyageai.Client(api_key=settings.voyage_api_key)
        res = _client.embed([text], model=settings.voyage_model, input_type=input_type,
                            output_dimension=settings.voyage_dims)
        _cache[key] = res.embeddings[0]
    return _cache[key]


def record(db, decision: Decision, query: StructuredQuery, who: Identity, project: str,
           now: datetime) -> None:
    """Remember a refused question (once per project, question and rule)."""
    label = describe(query)
    if db.precedents.find_one({"project": project, "label": label, "rule": decision.rule,
                               "overturned": False}, {"_id": 1}):
        return
    db.precedents.insert_one({
        "project": project, "overturned": False,
        "user": who.user, "team": who.team, "target": query.target,
        "label": label, "rule": decision.rule,
        "allowed_transforms": list(decision.allowed_transforms),
        "transform": None, "created_at": now, "resolved_at": None,
        "embedding": embed(label, "document"),
    })


def resolve(db, query: StructuredQuery, who: Identity, project: str, now: datetime) -> None:
    """A rewrite (query.transform set) was answered: note on this user's latest
    unresolved refusal of the same target which transform worked."""
    if not query.transform:
        return
    latest = db.precedents.find_one(
        {"project": project, "user": who.user, "target": query.target, "transform": None,
         "allowed_transforms": query.transform, "overturned": False},
        sort=[("created_at", -1), ("_id", -1)])
    if latest:
        db.precedents.update_one({"_id": latest["_id"]},
                                 {"$set": {"transform": query.transform, "resolved_at": now}})


def find_similar(db, query: StructuredQuery, project: str, k: int = 3) -> list[dict]:
    """$vectorSearch for earlier refusals similar to this question, in this project.
    Returns [{label, rule, transform, created_at, score}]: no values, no ids."""
    vector = embed(describe(query), "query")
    hits = db.precedents.aggregate([
        {"$vectorSearch": {"index": PRECEDENTS_VECTOR_INDEX, "path": "embedding",
                           "queryVector": vector, "numCandidates": 50, "limit": k,
                           "filter": {"project": project, "overturned": False}}},
        {"$project": {"_id": 0, "label": 1, "rule": 1, "transform": 1, "created_at": 1,
                      "score": {"$meta": "vectorSearchScore"}}},
    ])
    return [h for h in hits if h["score"] >= MIN_SCORE]


def overturn(db, project: str, label: str, reviewer: str, now: datetime) -> int:
    """Correction: a reviewer marks a precedent as wrong. Kept for history, never
    returned by find_similar again."""
    res = db.precedents.update_many(
        {"project": project, "label": label, "overturned": False},
        {"$set": {"overturned": True, "overturned_by": reviewer, "overturned_at": now}})
    return res.modified_count
