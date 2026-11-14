"""Tool definitions and handlers.

query_hr(query: StructuredQuery)
  1. validate (pydantic) → on error return a short message so the LLM can retry
  2. cells = query.executor.run_query(db, query, clock.now())
  3. history = memory.releases.load_history(...)   ([] when memory is off)
     updated_at = employees.updated_at[query.target] for every id in cells
                  ({} when memory is off or target is None)
  4. decision = gate.check(query, role, cells, history, updated_at, policy)
  5. ledger.append(...) always (kind "gate_decision", memory_on = ctx memory flag)
  6. allowed/partial → memory on:  memory.releases.record_then_release(...) then return cells
                       memory off: return cells WITHOUT recording a release
     blocked         → return evidence text, allowed_transforms,
                       and hints from memory.precedents.search(...)

Memory off (the left-hand control panel) neither READS nor WRITES releases, so the
control panel can never pollute the memory-on panel's history. It exists only to show
the attack succeeding without memory; it still runs L1-L2 and is still audited.

request_review(query, reason)
  → review.approvals.create(...); return approval id + "pending"
"""

from pydantic import ValidationError

from app import clock, gate
from app.db import get_db
from app.gate import evidence
from app.memory import ledger, precedents, releases
from app.policy import store
from app.query import executor
from app.review import approvals
from app.schemas import StructuredQuery


TOOL_SPECS: list[dict] = []   # TODO: build from StructuredQuery.model_json_schema()


def handle_query_hr(ctx: dict, args: dict) -> dict:
    raise NotImplementedError


def handle_request_review(ctx: dict, args: dict) -> dict:
    raise NotImplementedError
