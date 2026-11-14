"""Simulated clock so the demo can jump from Monday to Friday, or past an expiry."""
from datetime import datetime


def now() -> datetime:
    """Real time plus the current demo offset."""
    raise NotImplementedError


def advance(days: float) -> datetime:
    """Move the demo clock forward; return the new 'now'."""
    raise NotImplementedError
