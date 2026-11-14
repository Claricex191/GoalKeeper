"""FastAPI entry point.

Routes
- POST /chat              run the agent for one message (memory on/off)
- POST /review/{id}       reviewer approves or rejects a rewrite
- POST /admin/policy/draft               (stretch) agent drafts a policy for a dataset
- POST /admin/policy/{version}/activate  human approves a policy version
- POST /admin/clock       advance the simulated clock
- POST /admin/simulate-update  (demo) change one employee's sensitive field, e.g. a raise
- POST /admin/reset       wipe memory and re-run scripts/warmup.py
"""

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, HTTPException

from app import clock
from app.agent.loop import run_agent
from app.db import ensure_indexes, get_db
from app.memory import ledger, precedents, releases
from app.policy import profiler, store
from app.review import approvals
from app.schemas import ChatRequest, ChatResponse, Identity
from app.users import DEMO_USERS, current_identity, require_role


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes(get_db())          # validators + indexes; idempotent
    yield


app = FastAPI(title="Memory-Governed Privacy Agent", lifespan=lifespan)


@app.get("/demo/users")
def demo_users() -> list[dict]:
    """For the frontend's user switcher. Demo only: a real system has no such list."""
    return [asdict(u) for u in DEMO_USERS.values()]


@app.get("/me")
def me(who: Identity = Depends(current_identity)) -> dict:
    return asdict(who)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, who: Identity = Depends(current_identity)) -> ChatResponse:
    return run_agent(req, who)


@app.get("/review/pending")
def review_pending(who: Identity = Depends(current_identity)) -> list[dict]:
    """L5 review requests waiting for the HR head (frontend ReviewerPanel)."""
    require_role(who, "hr_head")
    return approvals.list_pending(get_db())


@app.post("/review/{approval_id}")
def review(approval_id: str, approve: bool, transform: str | None = None,
           who: Identity = Depends(current_identity)):
    """Approve or reject a pending review; either way the field is unlocked for that
    user and the L5 count restarts. A review never grants access the policy denies."""
    require_role(who, "hr_head")
    if not approvals.decide(get_db(), approval_id, approve, transform, who.user, clock.now()):
        raise HTTPException(404, "no pending review with this id")
    return {"ok": True}


@app.post("/admin/policy/draft")
def policy_draft(dataset: str):
    # STRETCH: policy.profiler.draft_policy(db, dataset) → returns draft version for review
    raise NotImplementedError


@app.post("/admin/policy/{version}/activate")
def policy_activate(version: int, dataset: str, approved_by: str):
    # policy.store.activate(...) – human action only
    raise NotImplementedError


DEMO_PROJECT = "comp-review-2026"      # the project the frontend uses


def _clock_view() -> dict:
    return {"now": clock.now().isoformat(), "offset_days": clock.offset_days()}


@app.get("/admin/clock")
def admin_clock_get() -> dict:
    return _clock_view()


@app.post("/admin/clock")
def admin_clock(days: float) -> dict:
    try:
        clock.advance(days)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _clock_view()


@app.post("/admin/simulate-update")
def admin_simulate_update(employee_number: int, field: str, new_value: float | int | str):
    """Demo stand-in for a real HR system write (e.g. a raise).

    - `field` must be one of the active policy's tracked_targets (else 400).
    - In one update on employees: set `<field>` = new_value AND
      `updated_at.<field>` = clock.now() (the simulated clock, so it is comparable
      with releases.created_at).
    - Append a ledger entry {kind: "data_update", employee_number, field, user, time},
      without the new value.

    In a real deployment this endpoint does not exist: an Atlas change stream or
    Atlas Trigger sets updated_at.<field> whenever a sensitive field is modified.
    """
    db = get_db()
    policy = store.load_active(db, "ibm_hr")
    if field not in policy.get("tracked_targets", policy.get("sensitive_targets", [])):
        raise HTTPException(400, f"{field} is not a tracked sensitive field")
    now = clock.now()
    res = db.employees.update_one({"EmployeeNumber": employee_number},
                                  {"$set": {field: new_value, f"updated_at.{field}": now}})
    if res.matched_count != 1:
        raise HTTPException(404, f"no employee {employee_number}")
    ledger.append(db, {"kind": "data_update", "time": now, "user": "demo-admin",
                       "employee_number": employee_number, "field": field})
    return {"ok": True}


@app.post("/admin/reset")
def admin_reset(project: str = DEMO_PROJECT) -> dict:
    """DEMO ONLY (a real deployment has no reset): start a rehearsal from a clean state.

    Clock back to Monday; this project's releases, review requests, precedents and
    gate decisions removed (L5 counts from the ledger, so a rehearsal would otherwise
    start already locked); in-process chat sessions cleared. Policy changes and data
    updates stay in the ledger. Employee data is not restored: re-run
    scripts/load_data.py after a simulate-update.
    """
    from app.agent import loop
    db = get_db()
    removed = {c: db[c].delete_many({"project": project}).deleted_count
               for c in ("releases", "approvals", "precedents")}
    removed["ledger"] = db.ledger.delete_many({"project": project,
                                               "kind": "gate_decision"}).deleted_count
    loop.SESSIONS.clear()
    clock.reset()
    return {"ok": True, **_clock_view(), "removed": removed}
