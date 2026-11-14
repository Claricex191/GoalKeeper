"""Executor vs pandas on the same CSV. Needs Atlas (backend/.env) with load_data run;
skipped otherwise. Passes `now` explicitly, so it never moves the shared demo clock."""
import operator
from pathlib import Path

import pytest

from app.clock import DEMO_START, NEW_HIRE_START
from app.query.executor import QueryError, build_pipeline, run_query
from app.policy.defaults import POLICY
from app.schemas import Identity, StructuredQuery

WHO = Identity("executor_test", "hr_head", "hr", "HR Head")

from app.demo.baseline import CSV_PATH as CSV   # data/goalkeeper_hr_2026.csv
NEW_HIRE_ID = 473
MON, THU = DEMO_START, NEW_HIRE_START


@pytest.fixture(scope="module")
def db():
    try:
        from app.db import get_db
        d = get_db()
        d.client.admin.command("ping", maxTimeMS=3000)
    except Exception as e:  # no .env, no network, wrong URI...
        pytest.skip(f"Atlas not reachable: {e}")
    if d.employees.estimated_document_count() == 0:
        pytest.skip("employees empty: run scripts/load_data.py")
    return d


@pytest.fixture(scope="module")
def df():
    if not CSV.exists():
        pytest.skip("run scripts/load_data.py --dry-run to create the CSV")
    pd = pytest.importorskip("pandas")
    d = pd.read_csv(CSV)
    d = d[d["active"] == "on"]                  # people who left are never queried
    low = d["Age"] // 5 * 5                     # same derivation as scripts/load_data.py
    d["AgeBand"] = low.astype(str) + "-" + (low + 4).astype(str)
    return d


PY_OPS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
          ">": operator.gt, ">=": operator.ge}


def pandas_cells(df, q: StructuredQuery, now):
    d = df if now >= NEW_HIRE_START else df[df.EmployeeNumber != NEW_HIRE_ID]
    for f in q.filters:
        d = d[d[f.col].isin(f.val)] if f.op == "in" else d[PY_OPS[f.op](d[f.col], f.val)]
    groups = d.groupby(q.group_by, observed=True) if q.group_by else [((), d)]
    out = {}
    for key, g in groups:
        key = key if isinstance(key, tuple) else (key,)
        value = None if q.metric == "count" else getattr(g[q.target], {"avg": "mean", "sum": "sum", "median": "median"}[q.metric])()
        out[key] = (set(g.EmployeeNumber), value)
    return out


SALES_MGR = [{"col": "Department", "op": "=", "val": "Sales"},
             {"col": "JobRole", "op": "=", "val": "Manager"}]
QUERIES = [
    {"filters": SALES_MGR, "metric": "avg", "target": "MonthlyIncome"},
    {"filters": SALES_MGR, "metric": "median", "target": "MonthlyIncome"},
    {"filters": SALES_MGR, "group_by": ["Gender"], "metric": "count"},
    {"filters": [{"col": "Department", "op": "=", "val": "Human Resources"},
                 {"col": "JobRole", "op": "=", "val": "Manager"}],
     "metric": "avg", "target": "MonthlyIncome"},
    {"filters": [{"col": "JobLevel", "op": "in", "val": [4, 5]}],
     "group_by": ["Department", "Gender"], "metric": "sum", "target": "MonthlyIncome"},
    {"filters": [{"col": "YearsAtCompany", "op": ">", "val": 0},
                 {"col": "YearsAtCompany", "op": "<=", "val": 5}],
     "group_by": ["AgeBand"], "metric": "avg", "target": "JobSatisfaction"},
    {"filters": [], "metric": "count"},
]


@pytest.mark.parametrize("now", [MON, THU], ids=["mon", "thu"])
@pytest.mark.parametrize("qd", QUERIES, ids=range(len(QUERIES)))
def test_matches_pandas(db, df, qd, now):
    q = StructuredQuery(**qd)
    got = {tuple(c.key[g] for g in q.group_by): c for c in run_query(db, q, now, WHO, POLICY)}
    want = pandas_cells(df, q, now)
    assert set(got) == set(want)
    for key, (ids, value) in want.items():
        c = got[key]
        assert c.ids == ids and c.n == len(ids)
        assert (c.value is None) if value is None else c.value == pytest.approx(value)


def test_demo_numbers(db):
    q = StructuredQuery(filters=SALES_MGR, metric="avg", target="MonthlyIncome")
    (mon,), (thu,) = run_query(db, q, MON, WHO, POLICY), run_query(db, q, THU, WHO, POLICY)
    assert (mon.n, round(mon.value, 2)) == (35, 16986.46)    # 2026 data, active only
    assert (thu.n, round(thu.value, 2)) == (36, 16861.94)
    assert thu.ids - mon.ids == {NEW_HIRE_ID}


def test_string_number_is_coerced(db):
    """LLMs often send "4" for an int column; must not silently return nothing."""
    as_str = StructuredQuery(filters=[{"col": "JobLevel", "op": "=", "val": "4"}], metric="count")
    as_int = StructuredQuery(filters=[{"col": "JobLevel", "op": "=", "val": 4}], metric="count")
    assert run_query(db, as_str, THU, WHO, POLICY)[0].ids == run_query(db, as_int, THU, WHO, POLICY)[0].ids


@pytest.mark.parametrize("qd, msg", [
    ({"filters": [{"col": "Salary", "op": "=", "val": 1}], "metric": "count"}, "not authorized"),
    ({"filters": [{"col": "$where", "op": "=", "val": 1}], "metric": "count"}, "not authorized"),
    ({"filters": [{"col": "JobLevel", "op": "=", "val": "high"}], "metric": "count"}, "expects int"),
    ({"filters": [{"col": "JobLevel", "op": "in", "val": 4}], "metric": "count"}, "non-empty list"),
    ({"metric": "avg", "target": "MonthlyIncome"}, "not numeric"),
    ({"metric": "avg"}, "not authorized"),
])
def test_rejects_bad_queries(qd, msg):
    types = {"JobLevel": int, "Attrition": str, "MonthlyIncome": str}
    with pytest.raises(QueryError, match=msg):
        build_pipeline(StructuredQuery(**qd), THU, types, WHO, POLICY)
