"""Exercise complementary suppression through the same interface as the query tool."""

from datetime import datetime, timezone

import pytest

from app.gate import check, evidence
from app.schemas import Cell, Identity, Release, StructuredQuery
from app.policy.defaults import POLICY

TARGET = "PerformanceRating"
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
HR_HEAD = Identity("test_hr", "hr_head", "hr", "HR Head")


def _cell(key, start, values):
    return Cell(key, set(range(start, start + len(values))), len(values),
                sum(values) / len(values), min(values), max(values))


def _total(cells):
    n = sum(c.n for c in cells)
    return Cell({}, set().union(*(c.ids for c in cells)), n,
                sum(c.n * c.value for c in cells) / n,
                min(c.vmin for c in cells), max(c.vmax for c in cells))


def _release(cell):
    return Release(cell.ids, TARGET, "avg", NOW, cell.key, "Earlier answer")


def _grouped(cells, group_by, history=()):
    query = StructuredQuery(metric="avg", target=TARGET, group_by=group_by)
    return check(query, HR_HEAD, cells, list(history), {}, POLICY)


@pytest.mark.parametrize("total_first", [False, True], ids=["grouped-first", "total-first"])
def test_sales_executive_ratings_do_not_compose_with_counts_and_total(total_first):
    # The actual IBM Sales Executive rating distributions by job level.
    cells = [
        _cell({"JobLevel": 2}, 0, [3] * 200 + [4] * 33),
        _cell({"JobLevel": 3}, 233, [3] * 71 + [4] * 8),
        _cell({"JobLevel": 4}, 312, [3] * 14),
    ]
    total_cell = _total(cells)
    total_query = StructuredQuery(metric="avg", target=TARGET)
    counts_query = StructuredQuery(metric="count", group_by=["JobLevel"])

    if total_first:
        total = check(total_query, HR_HEAD, [total_cell], [], {}, POLICY)
    else:
        grouped = _grouped(cells, ["JobLevel"])

    counts = check(counts_query, HR_HEAD, [Cell(c.key, c.ids, c.n, None) for c in cells],
                   [], {}, POLICY)
    if total_first:
        grouped = _grouped(cells, ["JobLevel"], [_release(c) for c in total.cells])
    else:
        total = check(total_query, HR_HEAD, [total_cell], [_release(c) for c in grouped.cells],
                      {}, POLICY)

    assert counts.status == total.status == "allowed"
    assert [c.n for c in counts.cells] == [233, 79, 14]
    assert grouped.status == "blocked"
    assert evidence.public_view(grouped)["cells"] == []


def test_multiple_homogeneous_groups_do_not_leave_other_ratings_visible():
    cells = [
        _cell({"JobLevel": 1}, 0, [3] * 5),
        _cell({"JobLevel": 2}, 5, [3] * 5),
        _cell({"JobLevel": 3}, 10, [3] * 4 + [4]),
    ]
    decision = _grouped(cells, ["JobLevel"])
    assert decision.status == "blocked"
    assert evidence.public_view(decision)["cells"] == []


def test_two_dimension_grouping_hides_all_ratings_with_marginal_totals():
    cells = [
        _cell({"Department": "Sales", "JobLevel": 1}, 0, [3] * 5),
        _cell({"Department": "Sales", "JobLevel": 2}, 5, [3] * 4 + [4]),
        _cell({"Department": "HR", "JobLevel": 1}, 10, [3] * 4 + [4]),
        _cell({"Department": "HR", "JobLevel": 2}, 15, [3] * 4 + [4]),
    ]
    sales = _total(cells[:2])
    sales.key = {"Department": "Sales"}
    hr = _total(cells[2:])
    hr.key = {"Department": "HR"}
    rows = _grouped([sales, hr], ["Department"])
    assert rows.status == "allowed"

    level_one = _total(cells[::2])
    level_one.key = {"JobLevel": 1}
    level_two = _total(cells[1::2])
    level_two.key = {"JobLevel": 2}
    history = [_release(c) for c in rows.cells]
    columns = _grouped([level_one, level_two], ["JobLevel"], history)
    assert columns.status == "allowed"

    history.extend(_release(c) for c in columns.cells)
    decision = _grouped(cells, ["Department", "JobLevel"], history)
    assert decision.status == "blocked"
    assert evidence.public_view(decision)["cells"] == []
