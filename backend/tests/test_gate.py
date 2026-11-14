"""Gate tests. Run from backend/: pytest -q

Two layers
1. Unit tests: hand-built Cells and Releases, one rule at a time, at its boundaries.
2. Scenario tests: demo acts and attacks on the real dataset, run step by step.
   Data: backend/data/ibm_hr.csv (create it with `python scripts/load_data.py --dry-run`;
   scenario tests are skipped while it is missing). Cases: cases/gate_cases.json (shared
   case file) plus GATE_SCENARIOS below (gate-specific attacks, kept here).

Case format
- "not_hired": [EmployeeNumber, ...]  absent from every population until a "hire" event
- "steps": run in order; each step is either a QUERY or an EVENT.

QUERY step
  {"user", "team", "date", "query": StructuredQuery, "expect": status,
   "expect_rule"?: e.g. "L4.pairwise", "expect_hidden"?: [cell key, ...] in hiding order,
   "memory"?: default true, "note"?: free text}

EVENT steps (change the simulated data; the gate itself never sees events)
  {"event": "hire",   "employee": 473, "date"}                  → joins populations
  {"event": "update", "employee": 1408, "field": "MonthlyIncome",
   "new_value": 18500, "date"}                                  → value changes and
                                                                  updated_at[field] = date

Harness (no database), mirroring agent.tools
- precheck first; a query it blocks never reaches the data
- cells: filter + group the CSV with the current state (hired set, updated values), with
  vmin / vmax for target queries; Attrition mapped Yes/No → 1/0 as the executor does
- updated_at: every update event so far ({} when memory is false)
- history: releases from earlier ALLOWED/PARTIAL memory-on TARGET steps of the SAME team
  (memory scope = team), with created_at = that step's date and user = that step's user;
  [] when memory is false. Memory-off steps and counts are never added.
"""

from __future__ import annotations

import json
import operator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import pandas as pd

from app.gate import check, cross_query, evidence, precheck, rules
from app.schemas import Cell, Filter, Identity, Release, StructuredQuery
from app.policy.access import authorized_departments
from app.policy.defaults import POLICY
from app.users import DEMO_USERS

INCOME = "MonthlyIncome"
REWRITES = ["T1_merge_groups", "T2_drop_filter", "T3_widen_scope"]
MON = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
WED = datetime(2026, 11, 11, 9, tzinfo=timezone.utc)
THU = datetime(2026, 11, 12, 9, tzinfo=timezone.utc)

HR_HEAD = Identity("test_hr", "hr_head", "hr", "HR Head")
MANAGER = Identity("test_mgr", "department_manager", "sales", "Sales Manager", ("Sales",))
EMPLOYEE = Identity("test_emp", "employee", "sales", "Employee", ("Sales",))
UNKNOWN = Identity("unknown", "intern", "sales", "Unknown", ("Sales",))


def query(metric="avg", target=INCOME, filters=(), group_by=()):
    return StructuredQuery(filters=[Filter(col=c, op=o, val=v) for c, o, v in filters],
                           group_by=list(group_by), metric=metric, target=target)


def cell(ids, key=None, vmin=1.0, vmax=2.0):
    ids = set(ids)
    return Cell(key=key or {}, ids=ids, n=len(ids), value=1.5, vmin=vmin, vmax=vmax)


def disjoint(sizes: dict, dim: str) -> list[Cell]:
    """One cell per value of `dim`, with disjoint populations of the given sizes."""
    cells, start = [], 0
    for value, n in sizes.items():
        cells.append(cell(range(start, start + n), {dim: value}))
        start += n
    return cells


def release(ids, at=MON, user="mgr_1", label="earlier", target=INCOME):
    return Release(ids=set(ids), target=target, metric="avg", created_at=at,
                   cell_key={}, label=label, user=user)


# --- L1 ------------------------------------------------------------------------------
@pytest.mark.parametrize("who, q, rule", [
    (MANAGER, query(filters=[("EmployeeNumber", "=", 1306)]), "L1.identifier"),
    (MANAGER, query(metric="count", target=None, group_by=["EmployeeNumber"]),
     "L1.identifier"),
    (HR_HEAD, query(target="EmployeeNumber"), "L1.identifier"),
    (MANAGER, query(filters=[("NewField", "=", 1)]), "L1.unlabelled"),
    (MANAGER, query(metric="count", target=None,
                    filters=[("MonthlyIncome", ">", 15000)]), "L1.filter_field"),
    (HR_HEAD, query(group_by=["PerformanceRating"]), "L1.filter_field"),
    (MANAGER, query(metric="count"), "L1.malformed"),
    (MANAGER, query(target=None), "L1.malformed"),
    (EMPLOYEE, query(), "L1.role"),
    (MANAGER, query(target="Age"), "L1.role"),
    (EMPLOYEE, query(metric="count", target=None), "L1.role"),
    (UNKNOWN, query(metric="count", target=None), "L1.role"),
    (HR_HEAD, query(metric="max", filters=[("Department", "=", "Research & Development")]),
     "L1.extreme"),
    (MANAGER, query(metric="min"), "L1.extreme"),
    (EMPLOYEE, query(metric="max"), "L1.role"),
])
def test_l1_rejects(who, q, rule):
    decision = precheck(q, who, POLICY)
    assert decision is not None
    assert (decision.status, decision.rule) == ("blocked", rule)


@pytest.mark.parametrize("who, q", [
    (MANAGER, query(filters=[("Department", "=", "Sales")], group_by=["Gender"])),
    (MANAGER, query(metric="sum")),
    (MANAGER, query(target="PerformanceRating")),
    (HR_HEAD, query(target="PerformanceRating", group_by=["JobLevel", "Gender"])),
    (HR_HEAD, query(target="DailyRate")),
    (HR_HEAD, query(target="YearsAtCompany")),
])
def test_l1_allows(who, q):
    assert precheck(q, who, POLICY) is None


def test_l1_transforms():
    assert precheck(query(metric="max"), HR_HEAD, POLICY).allowed_transforms == [
        "T4_extreme_to_avg"]
    assert precheck(query(), EMPLOYEE, POLICY).allowed_transforms == []


def test_check_repeats_l1_when_precheck_was_skipped():
    decision = check(query(filters=[("EmployeeNumber", "=", 1306)]), HR_HEAD,
                     [cell(range(5))], [], {}, POLICY)
    assert decision.rule == "L1.identifier"


# --- L2 ------------------------------------------------------------------------------
def test_l2_min_n_boundary():
    small, enough = cell(range(4), {"JobLevel": 1}), cell(range(10, 15), {"JobLevel": 2})
    kept, hidden = rules.cell_checks([small, enough], query(group_by=["JobLevel"]), POLICY)
    assert kept == [enough]
    assert hidden == [{"key": {"JobLevel": 1}, "reason": "L2.min_n", "n": 4}]


def test_l2_homogeneous_only_for_target_queries():
    same = cell(range(6), {"JobLevel": 4}, vmin=3, vmax=3)
    kept, hidden = rules.cell_checks([same], query(target="PerformanceRating"), POLICY)
    assert kept == [] and hidden[0]["reason"] == "L2.homogeneous"
    count = Cell(key={}, ids=set(range(6)), n=6, value=None)
    assert rules.cell_checks([count], query(metric="count", target=None), POLICY) == (
        [count], [])


def test_target_cells_without_vmin_vmax_fail_closed():
    bare = Cell(key={}, ids=set(range(6)), n=6, value=1.0)
    decision = check(query(), HR_HEAD, [bare], [], {}, POLICY)
    assert (decision.status, decision.rule) == ("blocked", "gate.error")


def test_empty_answer_is_allowed():
    assert check(query(), HR_HEAD, [], [release(range(36))], {}, POLICY).status == "allowed"


# --- L3 ------------------------------------------------------------------------------
def test_l3_hides_next_smallest_until_hidden_total_reaches_k():
    cells = disjoint({1: 33, 2: 13, 3: 6, 4: 4, 5: 7}, "JobLevel")   # HR in the real data
    decision = check(query(group_by=["JobLevel"]), HR_HEAD, cells, [], {}, POLICY)
    assert (decision.status, decision.rule) == ("partial", "L2.min_n")
    assert [(s["key"]["JobLevel"], s["reason"]) for s in decision.suppressed] == [
        (4, "L2.min_n"), (3, "L3.secondary")]


def test_l3_no_extra_hiding_once_hidden_total_is_k():
    cells = disjoint({1: 2, 2: 3, 3: 20}, "JobLevel")
    decision = check(query(group_by=["JobLevel"]), HR_HEAD, cells, [], {}, POLICY)
    assert [c.key for c in decision.cells] == [{"JobLevel": 3}]


def test_l3_two_cells_one_small_hides_both():
    cells = disjoint({"Female": 2, "Male": 5}, "Gender")
    decision = check(query(group_by=["Gender"]), HR_HEAD, cells, [], {}, POLICY)
    assert (decision.status, decision.rule) == ("blocked", "L2.min_n")
    assert decision.allowed_transforms == REWRITES


def test_l3_two_dimensions_checks_rows_and_columns():
    sizes = {("Sales", "Female"): 3, ("Sales", "Male"): 20,
             ("HR", "Female"): 30, ("HR", "Male"): 40}
    cells, start = [], 0
    for (dept, gender), n in sizes.items():
        cells.append(cell(range(start, start + n), {"Department": dept, "Gender": gender}))
        start += n
    decision = check(query(group_by=["Department", "Gender"]), HR_HEAD, cells, [], {},
                     POLICY)
    assert decision.status == "partial"
    assert [c.key for c in decision.cells] == [{"Department": "HR", "Gender": "Male"}]


# --- L4 pairwise ---------------------------------------------------------------------
@pytest.mark.parametrize("joined, blocked", [(0, False), (1, True), (4, True), (5, False)])
def test_pairwise_boundaries(joined, blocked):
    evidence_ = cross_query.pairwise([cell(range(36 + joined))], [release(range(36))],
                                     INCOME, {}, 5)
    assert (evidence_ is not None) == blocked


def test_pairwise_counts_value_changes_after_the_release_only():
    same = cell(range(11))
    raised = {3: {INCOME: WED}}
    evidence_ = cross_query.pairwise([same], [release(range(11), at=MON)], INCOME, raised, 5)
    assert (evidence_["n_membership_changed"], evidence_["n_value_changed"]) == (0, 1)
    assert cross_query.pairwise([same], [release(range(11), at=THU)], INCOME, raised,
                                5) is None


def test_pairwise_ignores_other_targets():
    old = release(range(36), target="JobSatisfaction")
    assert cross_query.pairwise([cell(range(37))], [old], INCOME, {}, 5) is None


def test_counts_skip_l4():
    count = Cell(key={}, ids=set(range(37)), n=37, value=None)
    decision = check(query(metric="count", target=None), HR_HEAD, [count],
                     [release(range(36))], {}, POLICY)
    assert decision.status == "allowed"


# --- L4 linear -----------------------------------------------------------------------
SALES_MGR = set(range(36))
NEW_HIRE = 1000
SALES_OTHER = set(range(2000, 2409))           # 409 non-managers
HR_MGR = set(range(100, 111))                  # 11 managers; 105 gets a raise on Wed
HR_OTHER = set(range(200, 252))                # 52 non-managers


def test_linear_new_hire_three_queries():
    history = [release(SALES_MGR, at=MON),
               release(SALES_MGR | {NEW_HIRE} | SALES_OTHER, at=THU)]
    third = cell(SALES_OTHER)
    assert cross_query.pairwise([third], history, INCOME, {}, 5) is None  # every pair passes
    evidence_ = cross_query.linear([third], history, INCOME, {})
    assert (evidence_["n_people"], evidence_["n_value_changed"]) == (1, 0)
    assert sorted(t["coefficient"] for t in evidence_["terms"]) == [-1, -1, 1]


def test_linear_raise_three_queries_needs_value_versions():
    raised = {105: {INCOME: WED}}
    history = [release(HR_MGR, at=MON), release(HR_MGR | HR_OTHER, at=THU)]
    third = cell(HR_OTHER)
    assert cross_query.pairwise([third], history, INCOME, raised, 5) is None
    evidence_ = cross_query.linear([third], history, INCOME, raised)
    assert (evidence_["n_people"], evidence_["n_value_changed"]) == (1, 1)
    assert cross_query.linear([third], history, INCOME, {}) is None  # population-only misses it


def test_linear_no_block_before_the_combination_is_complete():
    raised = {105: {INCOME: WED}}
    assert cross_query.check_history([cell(HR_MGR | HR_OTHER)], [release(HR_MGR, at=MON)],
                                     INCOME, raised, 5) is None


# --- public / audit views ------------------------------------------------------------
def test_public_view_never_exposes_ids_ranges_or_hiding_reasons():
    cells = disjoint({1: 33, 2: 13, 3: 6, 4: 4, 5: 7}, "JobLevel")
    view = evidence.public_view(check(query(group_by=["JobLevel"]), HR_HEAD, cells, [],
                                      {}, POLICY), viewer="mgr_1")
    text = json.dumps(view, default=str)
    assert set(view["cells"][0]) == {"key", "n", "value"}
    for leak in ('"ids"', "vmin", "vmax", "min_n", "secondary", "homogeneous", "n="):
        assert leak not in text
    assert view["rule"] == "L2"
    assert view["hidden"] == [{"JobLevel": 4}, {"JobLevel": 3}]
    assert view["evidence_text"] == (
        "Some groups are hidden to protect individual privacy: JobLevel=4; JobLevel=3.")


def test_pairwise_text_names_only_the_viewers_own_release():
    label = "Department=Sales, JobRole=Manager: avg MonthlyIncome"
    decision = check(query(), HR_HEAD, [cell(range(37))],
                     [release(range(36), user="mgr_1", label=label)], {}, POLICY)
    assert decision.rule == "L4.pairwise"
    assert evidence.render_public(decision, viewer="mgr_1") == (
        f"Since your earlier question on Mon 11/09 ('{label}'), fewer than 5 people in "
        "this group joined, left or had their data changed. Answering now would reveal "
        "their values.")
    teammate = evidence.render_public(decision, viewer="mgr_2")
    assert label not in teammate and "11/09" not in teammate
    assert teammate.startswith("Since an earlier answer given to your team,")
    audit = evidence.render_audit(decision)
    assert "1 joined/left, 0 value changes" in audit and "mgr_1" in audit


def test_linear_text_names_own_releases_only():
    history = [release(SALES_MGR, at=MON, user="mgr_1", label="A"),
               release(SALES_MGR | {NEW_HIRE} | SALES_OTHER, at=THU, user="mgr_2",
                       label="B")]
    decision = check(query(), HR_HEAD, [cell(SALES_OTHER)], history, {}, POLICY)
    assert decision.rule == "L4.linear"
    text = evidence.render_public(decision, viewer="mgr_1")
    assert "'A' on Mon 11/09" in text and "'B'" not in text
    assert "earlier answers given to your team" in text
    assert "isolates 1 employee(s), 0 of them by a value change" in evidence.render_audit(
        decision)


def test_l1_public_texts():
    extreme = precheck(query(metric="max"), HR_HEAD, POLICY)
    assert evidence.render_public(extreme).startswith(
        "The maximum or minimum of MonthlyIncome is never shown")
    assert evidence.render_public(precheck(query(), EMPLOYEE, POLICY)) == (
        "Your role (employee) cannot ask for avg of MonthlyIncome.")
    threshold = precheck(query(metric="count", target=None,
                               filters=[("MonthlyIncome", ">", 15000)]), HR_HEAD, POLICY)
    assert evidence.render_public(threshold).startswith(
        "MonthlyIncome cannot be used to filter or group results.")


def test_gate_error_is_generic_in_public_and_explained_in_audit():
    decision = check(query(), HR_HEAD, [Cell(key={}, ids=set(range(6)), n=6, value=1.0)],
                     [], {}, POLICY)
    assert evidence.render_public(decision) == evidence.GATE_ERROR
    assert "vmin" in evidence.render_audit(decision)


# --- Scenarios on the real dataset ---------------------------------------------------
DATA = Path(__file__).resolve().parents[1] / "data" / "ibm_hr.csv"
CASES = json.loads((Path(__file__).parent / "cases" / "gate_cases.json").read_text())
OPS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
       ">": operator.gt, ">=": operator.ge}


def where(col, val, op="="):
    return {"col": col, "op": op, "val": val}


def ask(user, team, date, expect, filters=(), group_by=(), metric="avg", target=INCOME,
        **extra):
    return {"user": user, "team": team, "date": date, "expect": expect,
            "query": {"filters": list(filters), "group_by": list(group_by),
                      "metric": metric, "target": target}, **extra}


SALES_MANAGERS = [where("Department", "Sales"), where("JobRole", "Manager")]
SALES_NON_MANAGERS = [where("Department", "Sales"), where("JobRole", "Manager", "!=")]
HR = [where("Department", "Human Resources")]
HR_MANAGERS = HR + [where("JobRole", "Manager")]
HR_NON_MANAGERS = HR + [where("JobRole", "Manager", "!=")]

GATE_SCENARIOS = [
    {"name": "Act 1 bypass – three answers isolate the new hire (L4.linear)",
     "not_hired": [473],
     "steps": [
         ask("mgr_1", "sales", "2026-11-09", "allowed", SALES_MANAGERS),
         {"event": "hire", "employee": 473, "date": "2026-11-12"},
         ask("mgr_1", "sales", "2026-11-12", "allowed", [where("Department", "Sales")],
             note="the agent's T3 rewrite after the Act 1 block"),
         ask("mgr_1", "sales", "2026-11-12", "blocked", SALES_NON_MANAGERS,
             expect_rule="L4.linear",
             note="all Sales − non-managers − Monday's managers = #473"),
         ask("mgr_1", "sales", "2026-11-12", "allowed", SALES_NON_MANAGERS, memory=False,
             note="control panel: without memory the attack succeeds"),
     ]},
    {"name": "Act 2 bypass – three answers isolate the raise (versioned L4.linear)",
     "not_hired": [],
     "steps": [
         ask("mgr_4", "hr", "2026-11-09", "allowed", HR_MANAGERS),
         {"event": "update", "employee": 1408, "field": INCOME, "new_value": 18500,
          "date": "2026-11-11"},
         ask("mgr_4", "hr", "2026-11-12", "allowed", HR,
             note="52 joined + 1 value change vs Monday: pairwise passes"),
         ask("mgr_4", "hr", "2026-11-12", "blocked", HR_NON_MANAGERS,
             expect_rule="L4.linear", note="all HR − non-managers − Monday = the raise"),
     ]},
    {"name": "Highest salary in R&D (L1.extreme), then the T4 rewrite",
     "not_hired": [],
     "steps": [
         ask("mgr_3", "rnd", "2026-11-09", "blocked",
             [where("Department", "Research & Development")], metric="max",
             expect_rule="L1.extreme"),
         ask("mgr_3", "rnd", "2026-11-09", "allowed",
             [where("Department", "Research & Development")], group_by=["JobLevel"],
             note="T4: average by JobLevel"),
     ]},
    {"name": "Permissions (L1)",
     "not_hired": [],
     "steps": [
         ask("emp_1", "sales", "2026-11-09", "blocked", [where("Department", "Sales")],
             expect_rule="L1.role"),
         ask("emp_1", "sales", "2026-11-09", "blocked", group_by=["Department"],
             metric="count", target=None, expect_rule="L1.role"),
         ask("mgr_1", "sales", "2026-11-09", "blocked", [where("EmployeeNumber", 1306)],
             expect_rule="L1.identifier"),
         ask("mgr_1", "sales", "2026-11-09", "blocked",
             [where("MonthlyIncome", 15000, ">")], metric="count", target=None,
             expect_rule="L1.filter_field"),
     ]},
    {"name": "HR executives by gender: 2 women hidden, so the 5 men too (L2 + L3)",
     "not_hired": [],
     "steps": [
         ask("mgr_4", "hr", "2026-11-09", "blocked", HR + [where("JobLevel", 5)],
             group_by=["Gender"], expect_rule="L2.min_n",
             expect_hidden=[{"Gender": "Female"}, {"Gender": "Male"}]),
         ask("mgr_4", "hr", "2026-11-09", "allowed", HR + [where("JobLevel", 5)],
             note="T1: merge the groups (7 people)"),
     ]},
    {"name": "HR by JobLevel: level 4 has 4 people, so level 3 is hidden too (L3)",
     "not_hired": [],
     "steps": [
         ask("mgr_4", "hr", "2026-11-09", "partial", HR, group_by=["JobLevel"],
             expect_rule="L2.min_n", expect_hidden=[{"JobLevel": 4}, {"JobLevel": 3}]),
         ask("mgr_4", "hr", "2026-11-09", "allowed", HR,
             note="total − shown levels = levels 3 and 4 together (10 people)"),
     ]},
    {"name": "Sales Executives' ratings: all 14 at level 4 are rated 3 (L2.homogeneous)",
     "not_hired": [],
     "steps": [
         ask("hr_1", "hr", "2026-11-09", "blocked",
             [where("JobRole", "Sales Executive")], group_by=["JobLevel"],
             target="PerformanceRating", expect_rule="L2.homogeneous",
             expect_hidden=[{"JobLevel": 4}, {"JobLevel": 2}, {"JobLevel": 3}]),
     ]},
]


def test_cases_load():
    assert isinstance(CASES, list)


@pytest.fixture(scope="module")
def employees() -> pd.DataFrame:
    if not DATA.exists():
        pytest.skip(f"{DATA} missing: run `python scripts/load_data.py --dry-run` first")
    pd = pytest.importorskip("pandas", reason="CSV scenario tests require pandas")
    df = pd.read_csv(DATA)
    df["AgeBand"] = [f"{a // 5 * 5}-{a // 5 * 5 + 4}" for a in df["Age"]]
    df["Attrition"] = (df["Attrition"] == "Yes").astype(int)
    return df.set_index("EmployeeNumber", drop=False)


@pytest.mark.parametrize("case", CASES + GATE_SCENARIOS, ids=lambda c: c["name"])
def test_scenario(case, employees):
    data = employees.copy()
    hired = set(data.index) - set(case["not_hired"])
    updated: dict = {}
    history: list[tuple[str, Release]] = []
    for number, step in enumerate(case["steps"]):
        when = datetime.fromisoformat(step["date"]).replace(hour=9, tzinfo=timezone.utc)
        if "event" in step:
            apply_event(step, when, data, hired, updated)
            continue
        q = StructuredQuery(**step["query"])
        who, memory = DEMO_USERS[step["user"]], step.get("memory", True)
        assert who.team == step["team"]
        past = [r for team, r in history if team == step["team"]] if memory else []
        decision = precheck(q, who, POLICY)
        if decision is None:
            rows = data.loc[sorted(hired)]
            departments = authorized_departments(who, POLICY)
            if departments is not None:
                rows = rows[rows.Department.isin(departments)]
            cells = build_cells(rows, q)
            decision = check(q, who, cells, past, updated if memory else {}, POLICY)
        context = f"step {number} ({step.get('note', '')}): {evidence.render_audit(decision)}"
        assert decision.status == step["expect"], context
        if "expect_rule" in step:
            assert decision.rule == step["expect_rule"], context
        if "expect_hidden" in step:
            assert [s["key"] for s in decision.suppressed] == step["expect_hidden"], context
        if memory and q.target is not None and decision.status in ("allowed", "partial"):
            history += [(step["team"], Release(
                ids=c.ids, target=q.target, metric=q.metric, created_at=when,
                cell_key=c.key, label=label(q, c), user=step["user"]))
                for c in decision.cells]


def apply_event(step: dict, when: datetime, data: pd.DataFrame, hired: set,
                updated: dict) -> None:
    employee = step["employee"]
    if step["event"] == "hire":
        hired.add(employee)
    elif step["event"] == "update":
        data.loc[employee, step["field"]] = step["new_value"]
        updated.setdefault(employee, {})[step["field"]] = when
    else:
        raise ValueError(f"unknown event {step['event']!r}")


def build_cells(rows: pd.DataFrame, q: StructuredQuery) -> list[Cell]:
    """What the executor returns: one cell per group, with ids, n, value, vmin, vmax."""
    for f in q.filters:
        column = rows[f.col]
        if f.op == "in":
            rows = rows[column.isin(f.val if isinstance(f.val, list) else [f.val])]
        else:
            rows = rows[OPS[f.op](column, f.val)]
    if q.group_by:
        groups = rows.groupby(list(q.group_by))
    else:
        groups = [((), rows)] if len(rows) else []
    cells = []
    for key, group in groups:
        key = key if isinstance(key, tuple) else (key,)
        values = group[q.target] if q.target else None
        cells.append(Cell(
            key={col: plain(v) for col, v in zip(q.group_by, key)},
            ids={int(i) for i in group["EmployeeNumber"]},
            n=len(group),
            value=None if values is None else float(
                values.mean() if q.metric == "avg" else values.sum()),
            vmin=None if values is None else float(values.min()),
            vmax=None if values is None else float(values.max()),
            departments=set(group["Department"])))
    return cells


def plain(value):
    return value.item() if hasattr(value, "item") else value


def label(q: StructuredQuery, c: Cell) -> str:
    parts = [f"{f.col}{f.op}{f.val}" for f in q.filters]
    parts += [f"{k}={v}" for k, v in c.key.items()]
    return f"{', '.join(parts) or 'everyone'}: {q.metric} {q.target}"
