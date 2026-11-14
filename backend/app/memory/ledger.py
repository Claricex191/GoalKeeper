"""Append-only audit log: every gate decision (allowed or not) plus governance events.

Every entry has `kind`; the validator checks required fields per kind
(full spec: docs/collections.md, ledger):
- "gate_decision"  {time, user, role, project, session_id, query, status, rule,
                    evidence_text, suppressed, policy_version}
- "data_update"    {time, user, employee_number, field}       (never the new value)
- "policy_change"  {time, user, dataset, from_version, to_version}
Viewed in the Atlas UI during the demo.
"""

from app import clock

KINDS = ("gate_decision", "data_update", "policy_change")


def append(db, entry: dict, session=None) -> None:
    """Insert one entry; `time` defaults to clock.now(). Pass `session` to write
    inside a caller's transaction."""
    if entry.get("kind") not in KINDS:
        raise ValueError(f"ledger entry needs kind in {KINDS}, got {entry.get('kind')!r}")
    doc = {"time": clock.now(), **entry}
    db.ledger.insert_one(doc, session=session)
