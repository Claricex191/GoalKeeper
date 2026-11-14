"""Clock validation and returned snapshots, without connecting to Atlas."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pymongo import ReturnDocument

from app import clock


@pytest.mark.parametrize("days", [0, -1, float("nan"), float("inf"), -float("inf"),
                                  1e100, 1e-15])
def test_invalid_advance_never_writes(days):
    db = SimpleNamespace(meta=Mock())
    with pytest.raises(ValueError):
        clock.advance(days, db=db)
    assert not db.meta.method_calls


def test_advance_uses_atomic_returned_offset():
    db = SimpleNamespace(meta=Mock())
    db.meta.find_one_and_update.return_value = {"offset_days": 3.5}
    session = object()
    assert clock.advance(1.5, db=db, session=session) == clock.DEMO_START + timedelta(days=3.5)
    db.meta.find_one.assert_not_called()
    args, kwargs = db.meta.find_one_and_update.call_args
    assert args[0]["offset_days"]["$lte"] == clock._MAX_OFFSET_DAYS - 1.5
    assert kwargs["return_document"] == ReturnDocument.AFTER
    assert kwargs["session"] is session


def test_cumulative_overflow_is_rejected():
    db = SimpleNamespace(meta=Mock())
    db.meta.find_one_and_update.return_value = None
    with pytest.raises(ValueError, match="exceed"):
        clock.advance(1, db=db)


def test_clock_reads_and_reset_use_callers_transaction():
    db = SimpleNamespace(meta=Mock())
    db.meta.find_one.return_value = {"offset_days": 2}
    session = object()
    assert clock.now(db=db, session=session) == clock.DEMO_START + timedelta(days=2)
    assert db.meta.find_one.call_args.kwargs["session"] is session
    assert clock.reset(db=db, session=session) == clock.DEMO_START
    assert db.meta.update_one.call_args.kwargs["session"] is session
