"""L4: does this answer, combined with what this team already knows, reveal someone?

Inputs
- history: the team's COMPLETE release history for the target (exact query, never vector
  search: top-k retrieval could miss the one release that completes the puzzle).
- updated_at: last-modified time of the target for EVERY employee that has one (sparse),
  not only the ids in the new cells: the linear check also needs it for ids that appear
  only in old releases.
Only target queries reach L4. Counts reveal no sensitive value and are never recorded.

Before comparing answers, reject connected histories whose value versions cannot be
determined from last-modified timestamps. Equal update/release timestamps do not order
the operations. Multiple observations before the latest update may span earlier updates.
These cases must not be merged into one "old" version. Disconnected histories are safe
to ignore for this ambiguity check because no population equation links them to a new cell.

Two complementary checks follow; pairwise first (cheap, easy to explain):
- pairwise (L4.pairwise): per (new cell, old release) on the same target, count the people
  whose contribution CHANGED between the two answers:
    A = membership changes: S_new Δ S_old (joined or left the population)
    B = value changes:      i in S_new ∩ S_old with updated_at[i][target] > old.created_at
  1 <= |A| + |B| <= k-1 → the difference of the two answers reveals the change of a small
  group → block. 0 or >= k → this pair passes. (A and B are disjoint by construction.)
- linear (L4.linear): sums and averages (avg × n) are linear in the individual values, so
  a set of answers reveals person i's value when e_i lies in the row space of the 0/1
  population matrix. Columns are (employee, value version): in an old release, an
  employee whose target changed after that release gets a separate "old" column. A
  revealed CHANGE, e_(i,cur) − e_(i,old), is caught too, e.g. a raise isolated by three
  answers that each pass pairwise:
      Mon  A = HR managers (11)
      Wed  one of them gets a raise
      Thu  B = all of HR;  C = HR non-managers      → B − C − A = that raise
  Block when the new cells make some value or change solvable that was not before.

Why B matters: a population can stay identical while one person's salary changes (e.g. a
raise). The same question asked before and after the raise passes a membership-only
check, yet the difference of the two averages reveals that raise.

Each old release's own created_at is the baseline for B. No time-window parameter.
Quasi-identifier changes (Department, JobLevel, Age...) need no timestamp: they move a
person in or out of a population, which A already catches.

Known limitations
- Three or more releases can reveal the TOTAL of a 2..k-1 person group (or the total of
  their changes) while every pair passes and no single value is solvable.
- updated_at keeps only the LAST change per field. Ambiguous histories are blocked even
  when a complete version log might establish safety. The timestamps and populations
  must describe the same data snapshot as the released answer.
"""

import numpy as np
from scipy.linalg import orth

from app.schemas import Cell, Release, UpdatedAt

TOL = 1e-8


def changed_after(updated_at: UpdatedAt, i: int, target: str, t) -> bool:
    """Strictly later; a missing entry means "not modified since load"."""
    ts = updated_at.get(i, {}).get(target)
    return ts is not None and ts > t


def _history_ambiguity(cells: list[Cell], history: list[Release], target: str | None,
                       updated_at: UpdatedAt) -> dict | None:
    """Fail closed when the connected equations need unavailable historical versions.

    One strictly pre-update observation per person has one well-defined old version.
    Two may straddle an earlier update, even if their release timestamps are identical.
    An observation at the update timestamp could be on either side of that update.
    Only employee sets are used for connectivity, conservatively including old versions.
    """
    if target is None or not cells:
        return None
    old = [r for r in history if r.target == target]
    by_employee: dict[int, list[int]] = {}
    for index, release in enumerate(old):
        for employee in release.ids:
            by_employee.setdefault(employee, []).append(index)
    frontier = {employee for cell in cells for employee in cell.ids}
    seen_people = set(frontier)
    connected: set[int] = set()
    while frontier:
        employee = frontier.pop()
        for index in by_employee.get(employee, []):
            if index in connected:
                continue
            connected.add(index)
            frontier.update(old[index].ids - seen_people)
            seen_people.update(old[index].ids)

    pre_update: set[int] = set()
    for index in sorted(connected):
        release = old[index]
        for employee in release.ids:
            updated = updated_at.get(employee, {}).get(target)
            if updated is None or updated < release.created_at:
                continue
            if updated == release.created_at:
                return {"rule": "L4.history", "reason": "update_release_timestamp_tie"}
            if employee in pre_update:
                return {"rule": "L4.history", "reason": "multiple_pre_update_observations"}
            pre_update.add(employee)
    return None


def describe(old: Release) -> dict:
    """What evidence may say about an old release. Never its ids."""
    return {"label": old.label, "cell_key": old.cell_key, "target": old.target,
            "metric": old.metric, "created_at": old.created_at, "n": len(old.ids),
            "user": old.user}


def pairwise(cells: list[Cell], history: list[Release], target: str | None,
             updated_at: UpdatedAt, k: int) -> dict | None:
    """Compare every new cell with every old release for the same target.

    Return evidence for the FIRST violation, else None. Counts only, never ids, so the
    evidence cannot itself disclose who changed:
      {"rule": "L4.pairwise", "k", "cell_key", "old_release": describe(old),
       "n_membership_changed": |A|, "n_value_changed": |B|, "n_changed": |A| + |B|}
    """
    if target is None:
        return None
    for cell in cells:
        for old in history:
            if old.target != target:
                continue
            a = cell.ids ^ old.ids
            b = {i for i in cell.ids & old.ids
                 if changed_after(updated_at, i, target, old.created_at)}
            changed = len(a) + len(b)
            if 1 <= changed <= k - 1:
                return {"rule": "L4.pairwise", "k": k, "cell_key": cell.key,
                        "old_release": describe(old),
                        "n_membership_changed": len(a), "n_value_changed": len(b),
                        "n_changed": changed}
    return None


def versioned_row(ids: set[int], target: str, updated_at: UpdatedAt, created_at) -> set:
    """Matrix columns for one answer: (i, "old") if i's target changed after this answer
    was released, else (i, "cur"). New cells pass created_at=None (current values)."""
    return {(i, "old") if created_at is not None
            and changed_after(updated_at, i, target, created_at) else (i, "cur")
            for i in ids}


def disclosures(rows: list[set]) -> set[tuple]:
    """What a set of sums over these populations reveals:
      ("value", col)  e_col is in the row space: one person's value (at that version)
      ("change", i)   e_(i,cur) − e_(i,old) is in the row space: one person's change
    With B an orthonormal basis of the row space (one row of B per column), x is in the
    row space iff |B^T x|^2 == |x|^2: |B[col]|^2 == 1 for a value,
    |B[cur] − B[old]|^2 == 2 for a change."""
    if not rows:
        return set()
    idx, matrix = _matrix(rows)
    basis = orth(matrix.T)
    found = {("value", c) for c, j in idx.items() if basis[j] @ basis[j] > 1 - TOL}
    for (i, version), j in idx.items():
        cur = idx.get((i, "cur"))
        if version == "old" and cur is not None:
            diff = basis[cur] - basis[j]
            if diff @ diff > 2 - TOL:
                found.add(("change", i))
    return found


def explain(rows: list[set], disclosure: tuple) -> list[float]:
    """Coefficients c (one per row) with sum(c_r * row_r) = the disclosed vector, by least
    squares; e.g. [-1, 1, -1] for B − C − A."""
    idx, matrix = _matrix(rows)
    x = np.zeros(len(idx))
    kind, what = disclosure
    if kind == "value":
        x[idx[what]] = 1.0
    else:
        x[idx[(what, "cur")]] = 1.0
        x[idx[(what, "old")]] = -1.0
    coef = np.linalg.lstsq(matrix.T, x, rcond=None)[0]
    return [round(float(c), 3) + 0.0 for c in coef]


def _matrix(rows: list[set]) -> tuple[dict, np.ndarray]:
    idx = {c: j for j, c in enumerate(sorted({c for row in rows for c in row}))}
    matrix = np.zeros((len(rows), len(idx)))
    for r, row in enumerate(rows):
        for c in row:
            matrix[r, idx[c]] = 1.0
    return idx, matrix


def linear(cells: list[Cell], history: list[Release], target: str | None,
           updated_at: UpdatedAt) -> dict | None:
    """Check historical versions, then test for newly solvable values or changes."""
    return (_history_ambiguity(cells, history, target, updated_at)
            or _linear(cells, history, target, updated_at))


def _linear(cells: list[Cell], history: list[Release], target: str | None,
            updated_at: UpdatedAt) -> dict | None:
    """On unambiguous histories, block newly solvable values or changes. Evidence:
      {"rule": "L4.linear", "n_people", "n_value_changed",
       "terms": [{"release": describe(old), "coefficient"} | {"this_answer": cell.key,
                 "coefficient"}]}   (the combination that isolates one of them)"""
    if target is None or not cells:
        return None
    old = [r for r in history if r.target == target]
    old_rows = [versioned_row(r.ids, target, updated_at, r.created_at) for r in old]
    rows = old_rows + [versioned_row(c.ids, target, updated_at, None) for c in cells]
    newly = disclosures(rows) - disclosures(old_rows)
    if not newly:
        return None
    coef = explain(rows, min(newly, key=repr))
    terms = [{"release": describe(r), "coefficient": c}
             for r, c in zip(old, coef) if c != 0]
    terms += [{"this_answer": cell.key, "coefficient": c}
              for cell, c in zip(cells, coef[len(old):]) if c != 0]
    changes = {what for kind, what in newly if kind == "change"}
    people = changes | {what[0] for kind, what in newly if kind == "value"}
    return {"rule": "L4.linear", "n_people": len(people),
            "n_value_changed": len(changes), "terms": terms}


def check_history(cells: list[Cell], history: list[Release], target: str | None,
                  updated_at: UpdatedAt, k: int) -> dict | None:
    """Check version ambiguity before pairwise (A + B) and linear disclosure checks."""
    return (_history_ambiguity(cells, history, target, updated_at)
            or pairwise(cells, history, target, updated_at, k)
            or _linear(cells, history, target, updated_at))
