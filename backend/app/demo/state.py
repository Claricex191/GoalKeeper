"""Serialize demo controls in each API process (MongoDB transactions protect writes)."""

from threading import RLock

operation_lock = RLock()
