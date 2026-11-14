"""FastAPI entry point.

Routes
- POST /chat              run the agent for one message (memory on/off)
- POST /review/{id}       reviewer approves or rejects a rewrite
- POST /admin/policy/draft               (stretch) agent drafts a policy for a dataset
- POST /admin/policy/{version}/activate  human approves a policy version
- GET /admin/clock        read the simulated clock
- POST /admin/clock       advance the simulated clock
- POST /admin/simulate-update  (demo) change one employee's sensitive field, e.g. a raise
- POST /admin/reset       restore the baseline and rebuild Monday's memory
"""

from contextlib import asynccontextmanager
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from math import isfinite

from fastapi import Depends, FastAPI, HTTPException, Query

from app import clock
from app.db import ensure_indexes, get_db
from app.demo.reset import reset_demo
from app.demo.state import operation_lock
from app.memory import ledger
from app.policy import store
from app.query import executor
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
    # TODO: call agent.loop.run_agent(req, who)
    raise NotImplementedError


@app.post("/review/{approval_id}")
def review(approval_id: str, approve: bool, transform: str | None = None,
           who: Identity = Depends(current_identity)):
    require_role(who, "hr")
    # TODO: review.approvals.decide(..., reviewer=who.user); on approve also precedents.save(...)
    raise NotImplementedError


@app.post("/admin/policy/draft")
def policy_draft(dataset: str, who: Identity = Depends(current_identity)):
    require_role(who, "hr")
    # STRETCH: policy.profiler.draft_policy(db, dataset) → returns draft version for review
    raise NotImplementedError


@app.post("/admin/policy/{version}/activate")
def policy_activate(version: int, dataset: str, who: Identity = Depends(current_identity)):
    require_role(who, "hr")
    with operation_lock:
        try:
            store.activate(get_db(), dataset, version, approved_by=who.user)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


def _clock_response(at):
    return {"now": at.isoformat(),
            "offset_days": (at - clock.DEMO_START).total_seconds() / 86400}


@app.get("/admin/clock")
def read_admin_clock(who: Identity = Depends(current_identity)):
    with operation_lock:
        return _clock_response(clock.now(db=get_db()))


@app.post("/admin/clock")
def admin_clock(days: float, who: Identity = Depends(current_identity)):
    with operation_lock:
        try:
            return _clock_response(clock.advance(days, db=get_db()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _update_value(field: str, raw: str, types: dict[str, type]):
    """Convert query text to a BSON-compatible value without silently changing type."""
    want = types.get(field)
    try:
        if want is int:
            value = Decimal(raw)
            if (not value.is_finite() or value != value.to_integral_value()
                    or not -(2 ** 63) <= value < 2 ** 63):
                raise ValueError
            return int(value)
        if want is float:
            value = float(raw)
            if not isfinite(value):
                raise ValueError
            return value
        if want is str and field == "Attrition" and raw in {"Yes", "No"}:
            return raw
    except (ValueError, InvalidOperation, OverflowError):
        pass
    if want is None:
        raise HTTPException(status_code=503, detail="employee field schema is not loaded")
    detail = ("Attrition must be Yes or No" if field == "Attrition"
              else f"{field} requires a finite {want.__name__} value")
    raise HTTPException(status_code=400, detail=detail)


@app.post("/admin/simulate-update")
def admin_simulate_update(field: str, new_value: str,
                          employee_number: int = Query(ge=-(2 ** 63), le=2 ** 63 - 1),
                          who: Identity = Depends(current_identity)):
    """Demo stand-in for a real HR system write (e.g. a raise).

    - `field` must be one of the active policy's sensitive_targets (else 400).
    - In one update on employees: set `<field>` = new_value AND
      `updated_at.<field>` = clock.now() (the simulated clock, so it is comparable
      with releases.created_at).
    - Append a ledger entry {kind: "data_update", employee_number, field, user, time},
      without the new value.

    In a real deployment this endpoint does not exist: an Atlas change stream or
    Atlas Trigger sets updated_at.<field> whenever a sensitive field is modified.
    """
    with operation_lock:
        db = get_db()

        def txn(session):
            # This shared document also serializes against CLI warmup, reset and
            # clock advances in other API processes. Read time after acquiring it.
            db.meta.update_one({"_id": "clock"},
                               {"$inc": {"data_generation": 1},
                                "$setOnInsert": {"offset_days": 0.0}},
                               upsert=True, session=session)
            try:
                policy = store.load_active(db, "ibm_hr", session=session)
            except LookupError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if (field not in policy["sensitive_targets"] or field.startswith("$")
                    or "." in field or field in executor.SYSTEM_FIELDS):
                raise HTTPException(status_code=400, detail="field must be a sensitive target")
            try:
                types = executor.field_types(db, session=session)
            except executor.QueryError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            value = _update_value(field, new_value, types)
            at = clock.now(db=db, session=session)
            if at <= clock.DEMO_START:
                raise HTTPException(status_code=409,
                                    detail="advance the demo clock before changing employee data")
            result = db.employees.update_one(
                {"EmployeeNumber": employee_number},
                {"$set": {field: value, f"updated_at.{field}": at}}, session=session,
            )
            if result.matched_count != 1:
                raise HTTPException(status_code=404, detail="employee not found")
            ledger.append(db, {"kind": "data_update", "employee_number": employee_number,
                               "field": field, "user": who.user, "time": at}, session=session)

        with db.client.start_session() as session:
            session.with_transaction(txn)
    return {"ok": True}


@app.post("/admin/reset")
def admin_reset(who: Identity = Depends(current_identity)):
    with operation_lock:
        try:
            at = reset_demo(get_db())
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "now": at.isoformat()}
