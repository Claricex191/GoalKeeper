"""Shared test setup.

Voyage is never called from tests: the free tier allows ~3 requests a minute and CI has
no key. `fake_embed` is deterministic (same text → same vector, so a repeated question
scores 1.0 in $vectorSearch) and has the index's dimension.
"""
import hashlib
import math

import pytest

from app.config import settings


def fake_embed(text: str, input_type: str = "document") -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    raw = [((seed[i % 32] * (i + 1)) % 251) / 250 - 0.5 for i in range(settings.voyage_dims)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


@pytest.fixture(autouse=True)
def _no_voyage(monkeypatch):
    from app.memory import precedents
    monkeypatch.setattr(precedents, "embed", fake_embed)
