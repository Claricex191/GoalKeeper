"""Replay "last week's" queries through the REAL pipeline before the demo.

Reads tests/cases/warmup.json, sets the simulated clock to each entry's date,
and calls the query_hr handler. Never writes to `releases` directly.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `app` importable

from app import clock  # noqa: E402
from app.agent import tools  # noqa: E402
from app.db import get_db  # noqa: E402


if __name__ == "__main__":
    raise NotImplementedError
