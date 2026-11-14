"""Validated GoalKeeper HR baseline (2026 snapshot). API reads local data only.

data/goalkeeper_hr_2026.csv = the 35 IBM HR Analytics columns (same 1,470 employees,
same EmployeeNumber) plus two columns added by the team, matched by EmployeeNumber:
- active                  "on" | "dimission" (left the company): drives the `active`
                          flag, so people who left are excluded from every query
- JobSatisfactionComment  free text, empty for most employees; measurement-report
                          category, individual reads only (no statistics on text)
Changes vs IBM: Human Resources sub-teams folded back into "Human Resources", one
R&D → Sales transfer, three HR job titles (HR Head, Salary Manager x2).
"""

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from app.clock import NEW_HIRE_START

CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "goalkeeper_hr_2026.csv"
EXPECTED_SHAPE = (1470, 37)
COMMENT = "JobSatisfactionComment"
ACTIVE_VALUES = {"on": True, "dimission": False}
NEW_HIRE_ID = 473
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


def check(df: pd.DataFrame) -> None:
    if df.shape != EXPECTED_SHAPE:
        raise ValueError(f"expected {EXPECTED_SHAPE}, got {df.shape}")
    if "EmployeeNumber" not in df.columns or not df["EmployeeNumber"].is_unique:
        raise ValueError("EmployeeNumber must be present and unique")
    if df.drop(columns=[COMMENT], errors="ignore").isna().any().any():
        raise ValueError("baseline must not contain missing values (except comments)")
    if "active" not in df.columns or not df["active"].isin(list(ACTIVE_VALUES)).all():
        raise ValueError(f"active must be one of {sorted(ACTIVE_VALUES)}")
    if COMMENT not in df.columns:
        raise ValueError(f"{COMMENT} column missing")
    hire = df[df["EmployeeNumber"] == NEW_HIRE_ID]
    if len(hire) != 1:
        raise ValueError(f"employee {NEW_HIRE_ID} missing")
    h = hire.iloc[0]
    if (h["Department"], h["JobRole"], h["YearsAtCompany"], h["active"]) != (
            "Sales", "Manager", 0, "on"):
        raise ValueError(f"employee {NEW_HIRE_ID} is not the expected Sales / Manager new hire")


def to_python(value):
    """numpy / pandas scalars to BSON-compatible Python values."""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def build_documents(df: pd.DataFrame) -> list[dict]:
    docs = []
    for row in df.to_dict("records"):
        doc = {key: to_python(value) for key, value in row.items()}
        low = doc["Age"] // 5 * 5
        comment = doc.get(COMMENT)
        doc[COMMENT] = comment if isinstance(comment, str) and comment.strip() else None
        doc.update(AgeBand=f"{low}-{low + 4}", active=ACTIVE_VALUES[doc["active"]],
                   updated_at={})
        doc["start_date"] = NEW_HIRE_START if doc["EmployeeNumber"] == NEW_HIRE_ID else LONG_AGO
        docs.append(doc)
    return docs


@lru_cache(maxsize=1)
def _read_baseline(path: str, mtime_ns: int, size: int) -> tuple[dict, ...]:
    # The file signature invalidates the cache when load_data refreshes the CSV.
    df = pd.read_csv(path)
    check(df)
    return tuple(build_documents(df))


def load_baseline() -> list[dict]:
    """Return an isolated copy; callers cannot mutate the cached baseline."""
    try:
        stat = CSV_PATH.stat()
        return deepcopy(_read_baseline(str(CSV_PATH), stat.st_mtime_ns, stat.st_size))
    except (OSError, ValueError, KeyError) as exc:
        raise LookupError("local employee baseline unavailable; run scripts/load_data.py first") from exc
