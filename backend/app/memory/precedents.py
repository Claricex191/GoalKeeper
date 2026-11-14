"""Reviewer precedents: the semantic memory.

Used only to suggest which rewrite to try first. A precedent can never make the gate
release something it would block.
"""

import voyageai
from bson import ObjectId

from app import clock
from app.config import settings


def embed(text: str) -> list[float]:
    """Voyage AI embedding of a request description."""
    raise NotImplementedError


def search(db, text: str, project: str, limit: int = 3) -> list[dict]:
    """$vectorSearch filtered by project and overturned == False."""
    raise NotImplementedError


def save(db, text: str, project: str, transform: str, reviewer: str, approved: bool) -> str:
    raise NotImplementedError


def overturn(db, precedent_id: str, reason: str, reviewer: str) -> None:
    """Correction: mark as overturned, keep history, link the replacing precedent."""
    raise NotImplementedError
