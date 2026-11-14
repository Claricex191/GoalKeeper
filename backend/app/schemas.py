"""Shared data contracts.

Agent, executor, gate and memory all speak these types. Agree on them on Friday;
change them only after telling the whole team.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# --- Whitelists ------------------------------------------------------------------
# NOT hard-coded here. Field lists, thresholds and role permissions come from the
# ACTIVE policy document at runtime (app/policy/store.py). Manual seeding and
# agent-drafted configs therefore go through the same path, with no code change.
# Use policy.store.load_active() and validate StructuredQuery against it.

Op = Literal["=", "!=", "<", "<=", ">", ">=", "in"]
Metric = Literal["count", "sum", "avg"]            # later: "median", "var"
Transform = Literal["T1_merge_groups", "T2_drop_filter", "T3_widen_scope"]
Status = Literal["allowed", "partial", "blocked"]


# --- What the LLM fills in (also used to generate the tool schema) --------------
class Filter(BaseModel):
    col: str
    op: Op
    val: str | int | float | list[str | int | float]


class StructuredQuery(BaseModel):
    filters: list[Filter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, max_length=2)
    metric: Metric
    target: str | None = None          # None only when metric == "count"
    transform: Transform | None = None # set when rewriting after a block
    # TODO: validators: columns in whitelists, target required unless count


# --- Gate input / output (pure Python, no DB) -----------------------------------
@dataclass
class Cell:
    key: dict                 # group values, e.g. {"Gender": "Female"}
    ids: set[int]             # population S (EmployeeNumber)
    n: int
    value: float | None       # None for pure counts


@dataclass
class Release:                # one row of the disclosure ledger (security memory)
    ids: set[int]
    target: str
    metric: str
    created_at: datetime      # release time; also the baseline for B-type (value) changes
    cell_key: dict            # group values of the released cell, e.g. {"Gender": "Female"}
    label: str                # fixed-format description for evidence text, e.g.
                              # "Sales / Manager, average MonthlyIncome" (no values)


# Per-employee, per-sensitive-field last-modified time, from employees.updated_at:
#   {EmployeeNumber: {"MonthlyIncome": datetime, ...}}
# A missing employee or field means "not modified since load". Only sensitive targets
# are tracked (see docs/collections.md, employees.updated_at).
UpdatedAt = dict[int, dict[str, datetime]]


@dataclass
class Decision:
    status: Status
    cells: list[Cell] = field(default_factory=list)       # safe to return
    suppressed: list[dict] = field(default_factory=list)  # {key, reason}
    evidence: dict | None = None                          # why blocked (for templates)
    rule: str | None = None                               # e.g. "L4.pairwise"
    allowed_transforms: list[Transform] = field(default_factory=list)


# --- Who is asking (resolved server-side, see app/users.py) ---------------------
Role = Literal["manager", "hr", "employee"]


@dataclass(frozen=True)
class Identity:
    user: str
    role: Role
    team: str                 # memory scope boundary when settings.memory_scope == "team"
    display_name: str


# --- API ------------------------------------------------------------------------
class ChatRequest(BaseModel):
    # No user / role / team here: identity comes from the X-Demo-User header and
    # app.users.DEMO_USERS, never from the request body.
    project: str
    session_id: str
    message: str
    memory: bool = True       # False = left-hand "no memory" panel


class ChatResponse(BaseModel):
    reply: str
    trace: list[dict]         # one entry per tool call: query, decision, rewrite
