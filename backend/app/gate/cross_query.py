"""L4: does this answer, combined with what this user already knows, reveal someone?

Must receive the user's COMPLETE release history for the target (exact query, never
vector search: top-k retrieval could miss the one release that completes the puzzle).

Two complementary checks:
- pairwise:  per (new cell, old release) on the same sensitive target, count the people
             whose contribution CHANGED between the two answers:
               A = membership changes: S_new Δ S_old (joined or left the population)
               B = value changes:      i in S_new ∩ S_old with
                                       updated_at[i][target] > old_release.created_at
             1 <= |A| + |B| <= k-1 → the difference of the two answers reveals the
             change of a small group → block. 0 or >= k → this pair passes.
             (A and B are disjoint by construction.)
- linear:    some e_i newly in rowspace(A) → one person's value is solvable (linear stats).
             Uses population sets only; value changes (B) are not modelled here.

Why B matters: a population can stay identical while one person's salary changes
(e.g. a raise). The same question asked before and after the raise passes a
membership-only check, yet the difference of the two averages reveals that raise.

Each old release's own created_at is the baseline for B. No time-window parameter.
Quasi-identifier changes (Department, JobLevel, Age...) need no timestamp: they move a
person in or out of a population, which A already catches.

Known limitation: three or more releases can combine to reveal the TOTAL of a 2..k-1
person group while every pair passes and no single e_i is solvable.
"""

import numpy as np
from scipy.linalg import null_space

from app.schemas import Cell, Release, UpdatedAt


def pairwise(cells: list[Cell], history: list[Release], target: str | None,
             updated_at: UpdatedAt, k: int) -> dict | None:
    """Compare every new cell with every old release for the same target.

    For each (cell, release) pair:
      A = cell.ids Δ release.ids
      B = {i in cell.ids ∩ release.ids : updated_at[i][target] > release.created_at}
          (strictly later; missing entry = unchanged; B is empty when target is None,
          i.e. a pure count, because no sensitive value is released)
      changed = |A| + |B|
      1 <= changed <= k-1 → violation; 0 or >= k → continue with the next pair.

    Return evidence for the FIRST violation, else None. Evidence carries counts only,
    never ids, so it cannot itself disclose who changed:
      {"rule": "L4.pairwise",
       "cell_key": cell.key,
       "old_release": {"label", "cell_key", "target", "metric", "created_at", "n"},
       "n_membership_changed": |A|,
       "n_value_changed": |B|,
       "n_changed": |A| + |B|}
    """
    raise NotImplementedError


def solvable_ids(pop_sets: list[set[int]]) -> set[int]:
    """Employees whose value is uniquely determined by these population sums.
    Columns = union of ids; rows = 0/1 indicators; N = scipy.linalg.null_space(A);
    i is solvable iff all |N[i, :]| < 1e-9."""
    raise NotImplementedError


def explain(pop_sets: list[set[int]], i: int) -> list[float]:
    """Coefficients c with A^T c = e_i (least squares), e.g. [1, -1, -1]."""
    raise NotImplementedError


def check_history(cells: list[Cell], history: list[Release], target: str | None,
                  updated_at: UpdatedAt, k: int) -> dict | None:
    """pairwise (A + B) first (cheap, explainable), then linear. Return evidence or None."""
    raise NotImplementedError
