"""Approval requests and the kill switch, via change streams."""

from bson import ObjectId

from app import clock


def create(db, user: str, project: str, query: dict, reason: str) -> str:
    """Insert {status: "pending", ...}; return id."""
    raise NotImplementedError


def decide(db, approval_id: str, approve: bool, transform: str | None, reviewer: str) -> None:
    raise NotImplementedError


def watch(db, on_change) -> None:
    """Run a change stream on approvals; call on_change(doc) for decisions and kill switch."""
    raise NotImplementedError
