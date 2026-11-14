"""Server-owned data scope; independent of the release-history sharing scope."""
from app.schemas import Identity


def authorized_departments(who: Identity, policy: dict) -> tuple[str, ...] | None:
    """None means company-wide. Missing/legacy identities and scopes fail closed.

    These departments must come from the authenticated directory, never from a query.
    ``who.team`` is deliberately not used: it controls disclosure-memory sharing only.
    """
    if not isinstance(who, Identity) or policy.get("schema_version") != 2:
        raise PermissionError("a server-resolved identity and v2 policy are required")
    permissions = policy["roles"].get(who.role)
    if not permissions:
        raise PermissionError("unknown role")
    scope = permissions.get("scope")
    if scope == "company":
        return None
    if scope not in ("assigned_department", "own_hr_department"):
        raise PermissionError("this role has no data scope")
    departments = who.departments
    if (not isinstance(departments, tuple) or not departments
            or any(not isinstance(d, str) or not d.strip() or d == "*" for d in departments)):
        raise PermissionError("an assigned department is required")
    if not set(departments).issubset(policy["departments"]):
        raise PermissionError("unknown assigned department")
    if scope == "own_hr_department" and not set(departments).issubset(policy["hr_departments"]):
        raise PermissionError("ordinary HR is restricted to its own HR department")
    return tuple(sorted(set(departments)))


def permits_field(who: Identity, policy: dict, field: str, mode: str) -> bool:
    """Business categories and privacy-risk labels are separate policy dimensions."""
    category = policy["field_categories"].get(field)
    grants = policy["roles"].get(who.role, {}).get("grants", {})
    return category is not None and mode in grants.get(category, [])
