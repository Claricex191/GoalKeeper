"""Who is asking: the demo user directory.

The client sends only a user id (header `X-Demo-User`); role and team always come from
this server-side table, so a client cannot claim a role or team it does not have.
Memory scope (settings.memory_scope = team) and L1 role permissions both depend on it.

The same ids and teams are used in tests/cases/gate_cases.json
(tests/test_users.py checks that they agree).

Real deployment: replace current_identity() with SSO / JWT verification and read
role + team from the HR system. Nothing else changes.
"""

from fastapi import Header, HTTPException

from app.schemas import Identity


DEMO_USERS: dict[str, Identity] = {u.user: u for u in [
    Identity("mgr_1", "manager", "sales", "Manager 1 (Sales)"),
    Identity("mgr_2", "manager", "sales", "Manager 2 (Sales)"),
    Identity("mgr_3", "manager", "rnd",   "Manager 3 (R&D)"),
    Identity("mgr_4", "manager", "hr",    "Manager 4 (HR dept.)"),
    Identity("hr_1",  "hr",      "hr",    "HR analyst / reviewer"),
]}


def resolve(user: str) -> Identity | None:
    return DEMO_USERS.get(user)


def current_identity(x_demo_user: str | None = Header(default=None)) -> Identity:
    """FastAPI dependency: unknown or missing user → 401 (fail closed)."""
    who = resolve(x_demo_user) if x_demo_user else None
    if who is None:
        raise HTTPException(status_code=401, detail="unknown user (send X-Demo-User)")
    return who


def require_role(who: Identity, *roles: str) -> None:
    if who.role not in roles:
        raise HTTPException(status_code=403, detail=f"role {who.role!r} may not do this")
