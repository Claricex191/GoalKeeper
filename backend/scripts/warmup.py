"""Seed both Monday disclosures through the real executor, gate and ledger.

Run after scripts/load_data.py and scripts/seed_policies.py on a clean Monday.
Repeated calls are rejected; /admin/reset restores the data and seeds again.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `app` importable

from app.db import get_db  # noqa: E402
from app.demo.warmup import run_warmup  # noqa: E402


if __name__ == "__main__":
    try:
        count = run_warmup(get_db())
    except (ValueError, LookupError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"warmup complete: {count} Monday releases for sales and hr")
