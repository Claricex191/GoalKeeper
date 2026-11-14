"""The privacy gate. Pure functions: no database, no LLM, no clock.

Owner: Wenqian. Can be written and tested offline against the CSV.
"""

from app.gate import cross_query, rules
from app.schemas import Cell, Decision, Release, StructuredQuery, UpdatedAt


def check(query: StructuredQuery, role: str, cells: list[Cell],
          history: list[Release], updated_at: UpdatedAt, policy: dict) -> Decision:
    """Run L1 → L2 → L3 → L4 in order.

    L1 rules.field_policy        reject the whole query
    L2 rules.cell_checks         suppress single cells
    L3 rules.complementary       suppress one more cell if a total would reveal it
    L4 cross_query.check_history block the whole query, with evidence;
                                 counts both membership changes (A) and sensitive-value
                                 changes since each old release (B)
    Suppressed cells never reach L4 and are never recorded.
    Any exception → blocked (fail closed).

    updated_at: per-employee last-modified time of each sensitive field, for (at least)
    every id in `cells` and query.target. Supplied by the caller (agent.tools), read from
    employees.updated_at; passed in so the gate stays free of database access.
    When memory is off, history is [] and updated_at may be {}.
    """
    raise NotImplementedError
