"""Load IBM HR Analytics into Atlas.

- read backend/data/ibm_hr.csv if present, else
  sklearn.datasets.fetch_openml(data_id=43896, as_frame=True) and save that CSV
- check: 1,470 rows, 35 columns, EmployeeNumber present and unique
- convert numpy / categorical values to plain Python int / str before inserting
- add per employee:
  - `active` = True
  - `AgeBand` (5-year bands, e.g. "25-29")
  - `start_date` = a date well before the demo (2000-01-01)
  - `updated_at` = {}   (no sensitive field modified since load)
- new-hire demo: EmployeeNumber 473 (Sales / Manager, YearsAtCompany == 0) gets
  start_date = clock.NEW_HIRE_START (Thursday), so the warmup "Monday" queries
  run without them and advancing the clock adds them. Every other employee, including
  1306 (R&D / Manager, used in the three-query demo), is present from the start.
- insert into `employees` (re-running REPLACES the collection);
  create the indexes listed in docs/collections.md

Run from backend/:
  python scripts/load_data.py --dry-run   # no database: download, check, print summary
  python scripts/load_data.py             # load into Atlas (needs MONGODB_URI in .env)
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from pymongo import ASCENDING

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `app` importable

from app.clock import DEMO_START, NEW_HIRE_START  # noqa: E402

CSV_PATH = Path(__file__).resolve().parents[1] / "data" / "ibm_hr.csv"
OPENML_ID = 43896
EXPECTED_SHAPE = (1470, 35)
NEW_HIRE_ID = 473
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


def read_dataset() -> pd.DataFrame:
    """Local CSV if present; otherwise download from OpenML and cache it."""
    if not CSV_PATH.exists():
        from sklearn.datasets import fetch_openml

        print(f"downloading OpenML {OPENML_ID} ...")
        df = fetch_openml(data_id=OPENML_ID, as_frame=True).frame
        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(CSV_PATH, index=False)
        print(f"saved {CSV_PATH}")
    # Always read the CSV, so dtypes are identical whether cached or just downloaded.
    print(f"reading {CSV_PATH}")
    return pd.read_csv(CSV_PATH)


def check(df: pd.DataFrame) -> None:
    assert df.shape == EXPECTED_SHAPE, f"expected {EXPECTED_SHAPE}, got {df.shape}"
    assert "EmployeeNumber" in df.columns, "EmployeeNumber missing"
    assert df["EmployeeNumber"].is_unique, "EmployeeNumber not unique"
    hire = df[df["EmployeeNumber"] == NEW_HIRE_ID]
    assert len(hire) == 1, f"employee {NEW_HIRE_ID} missing"
    h = hire.iloc[0]
    assert (h["Department"], h["JobRole"], h["YearsAtCompany"]) == ("Sales", "Manager", 0), \
        f"employee {NEW_HIRE_ID} is not the Sales / Manager new hire the demo expects"


def to_python(v):
    """numpy / pandas scalars → plain Python types that pymongo can encode."""
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def build_documents(df: pd.DataFrame) -> list[dict]:
    docs = []
    for row in df.to_dict("records"):
        doc = {k: to_python(v) for k, v in row.items()}
        low = doc["Age"] // 5 * 5
        doc["AgeBand"] = f"{low}-{low + 4}"
        doc["active"] = True
        doc["start_date"] = NEW_HIRE_START if doc["EmployeeNumber"] == NEW_HIRE_ID else LONG_AGO
        doc["updated_at"] = {}
        docs.append(doc)
    return docs


def summarize(docs: list[dict]) -> None:
    """Print the demo numbers so anyone can check the load matches the test cases."""
    print(f"documents: {len(docs)}  fields per doc: {len(docs[0])}")
    for label, at in [("Mon", DEMO_START), ("Thu", NEW_HIRE_START)]:
        g = [d for d in docs if d["Department"] == "Sales" and d["JobRole"] == "Manager"
             and d["start_date"] <= at]
        avg = sum(d["MonthlyIncome"] for d in g) / len(g)
        print(f"  Sales / Manager on {label}: n={len(g)}  avg MonthlyIncome={avg:.2f}")
    left = {k for d in docs for k, v in d.items() if type(v).__module__.startswith("numpy")}
    assert not left, f"numpy values left in: {left}"


def load(docs: list[dict]) -> None:
    from app.db import get_db  # imported here so --dry-run works without .env

    db = get_db()
    db.employees.drop()
    db.employees.insert_many(docs)
    db.employees.create_index([("EmployeeNumber", ASCENDING)], unique=True)
    db.employees.create_index([("Department", ASCENDING), ("JobLevel", ASCENDING),
                               ("Gender", ASCENDING)])
    n = db.employees.count_documents({})
    hire = db.employees.find_one({"EmployeeNumber": NEW_HIRE_ID}, {"start_date": 1})
    print(f"loaded {n} employees into {db.name}.employees; "
          f"#{NEW_HIRE_ID} start_date = {hire['start_date']}")


if __name__ == "__main__":
    df = read_dataset()
    check(df)
    docs = build_documents(df)
    summarize(docs)
    if "--dry-run" in sys.argv:
        print("dry run: nothing written")
    else:
        load(docs)
