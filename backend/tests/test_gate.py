"""Gate unit tests driven by case files.

Each case = a sequence of structured queries by one or more users, with the expected
status of each step. Run with: pytest -q
"""
import json
from pathlib import Path

CASES = json.loads((Path(__file__).parent / "cases" / "gate_cases.json").read_text())


def test_cases_load():
    assert isinstance(CASES, list)

# TODO: parametrize over CASES; build cells from the CSV; call gate.check step by step,
#       feeding each allowed step back in as history; assert expected status and rule.
