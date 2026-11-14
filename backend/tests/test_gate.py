"""Gate unit tests driven by case files (cases/gate_cases.json). Run with: pytest -q

Case format
- "not_hired": [EmployeeNumber, ...]  absent from every population until a "hire" event
- "steps": run in order; each step is either a QUERY or an EVENT.

QUERY step
  {"user", "team", "date", "query": StructuredQuery, "expect": status,
   "expect_rule"?: e.g. "L4.pairwise", "memory"?: default true, "note"?: free text}

EVENT steps (change the simulated data; the gate itself never sees events)
  {"event": "hire",   "employee": 473, "date"}                  → joins populations
  {"event": "update", "employee": 1408, "field": "MonthlyIncome",
   "new_value": 18500, "date"}                                  → value changes and
                                                                  updated_at[field] = date

Harness (no database; data from backend/data/ibm_hr.csv)
- cells: filter + group the CSV with the current state (hired set, updated values)
- updated_at: built from the update events so far ({} when memory is false)
- history: releases from earlier ALLOWED/PARTIAL memory-on steps of the SAME team
  (memory scope = team), each with created_at = that step's date;
  [] when memory is false, and memory-off steps are never added to history
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from app.gate import check
from app.schemas import Cell, Release, StructuredQuery


CASES = json.loads((Path(__file__).parent / "cases" / "gate_cases.json").read_text())


def test_cases_load():
    assert isinstance(CASES, list)

# TODO: parametrize over CASES; build cells from the CSV; call gate.check step by step,
#       feeding each allowed step back in as history; assert expected status and rule.
