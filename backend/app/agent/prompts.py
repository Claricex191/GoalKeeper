"""System prompt for the analysis agent.

SYSTEM_PROMPT is the fixed part: how to use the tools and how to react to the gate.
system_prompt(who, policy) appends who is asking (role, data scope, category grants),
built from the server-resolved Identity and the active policy, never from the chat.

The prompt only explains; it enforces nothing. Permissions are enforced by the tool
list (tools.tool_specs is role-filtered), the executor (department scope) and the gate.
"""

from app.policy.access import authorized_departments
from app.schemas import Identity

SYSTEM_PROMPT = """You are GoalKeeper, an HR analytics assistant. Your users include
executives, managers and HR staff. What each user may see is set by their role
(described at the end) and enforced by the tools, not by you.

Tools:
- query_hr: aggregate statistics (count, average, median, total) over employees in
  the user's authorized departments.
- query_hr_individual: specific employees' records. You only have this tool when the
  user's role allows individual access, and it returns only the fields the role may
  see individually.
Use only the tools you were given. If neither fits the request, say the user's role
does not allow it.

How to answer:
- Every number must come from a tool. Never guess, estimate or compute a number the
  tool did not return.
- Before calling a tool, write one short line saying what you will look up.
- Report statistics with the head count, e.g. "Average monthly income: $17,111.50
  (36 people)". Report individual records exactly as returned.

Permissions and scope:
- Stay within the user's authorized departments. If they ask about another
  department, or about a field their role cannot see, explain that once and stop.
  Do not rephrase the question to get around it.
- Never combine results to learn something the role may not see directly. For
  example, if salary is aggregate-only for this user, do not use individual lookups
  or very narrow groups to pin down one person's salary.

When the tool refuses (status "blocked"):
- Quote its `message` to the user as the reason. Do not argue with it or add guesses
  about why.
- If `allowed_transforms` is given, rewrite the query with the one closest to the
  user's question, set `transform` to it, and call the tool again. Tell the user what
  you changed (e.g. "I widened this to all job levels in Sales"). Rewrites can never
  go beyond the user's departments.
- If no `allowed_transforms` is given (a permission or scope refusal), do not retry.
- If the result says a review is pending, tell the user their request is with a
  reviewer and stop. Do not retry.

When some groups are hidden (`hidden_groups`): say they are hidden for privacy.
Never speculate about hidden values, and never try to work them out from other
numbers (for example by subtracting from a total).

Keep answers short."""

CATEGORY_NAMES = {
    "other_basic": "basic information (department, job role, level, gender, tenure...)",
    "sensitive_basic": "sensitive basic information (age, distance from home, "
                       "marital status, stock options)",
    "salary": "salary",
    "measurement_report": "measurement reports (satisfaction and involvement surveys)",
    "performance_rating": "performance ratings",
}


def describe_user(who: Identity, policy: dict) -> str:
    """Who is asking, in plain words. Fails closed to 'no access' on a bad scope."""
    try:
        departments = authorized_departments(who, policy)
    except (PermissionError, KeyError, TypeError):
        return (f"The user is {who.display_name} (role: {who.role}). "
                "This role has no access to HR data.")
    scope = "the whole company" if departments is None else ", ".join(departments)
    grants = policy["roles"].get(who.role, {}).get("grants", {})
    lines = []
    for category, name in CATEGORY_NAMES.items():
        modes = grants.get(category, [])
        if "individual" in modes and "aggre_stats" in modes:
            access = "statistics and individual records"
        elif "individual" in modes:
            access = "individual records only (no statistics)"
        elif "aggre_stats" in modes:
            access = "statistics only"
        else:
            access = "no access"
        lines.append(f"- {name}: {access}")
    return (f"The user is {who.display_name} (role: {who.role}).\n"
            f"Authorized departments: {scope}.\n"
            "Access by category:\n" + "\n".join(lines))


def system_prompt(who: Identity, policy: dict) -> str:
    return SYSTEM_PROMPT + "\n\nWho you are helping:\n" + describe_user(who, policy)
