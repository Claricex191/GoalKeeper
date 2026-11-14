# Frontend ↔ backend contract

The browser calls `/api/...`; Vite proxies it to `http://localhost:8000/...`.
Every call sends `X-Demo-User: <user id>`. The backend resolves role and team itself.

## Endpoints

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| GET | `/demo/users` | | `[{user, role, team, display_name}]` |
| POST | `/chat` | `{project, session_id, message, memory}` | `ChatResponse` (below) |
| GET | `/admin/clock` | | `{now: ISO string, offset_days: number}` |
| POST | `/admin/clock` | `?days=3` | `{now, offset_days}` |
| POST | `/admin/simulate-update` | `?employee_number=1408&field=MonthlyIncome&new_value=18500` | `{ok: true}` |
| POST | `/admin/reset` | | `{ok: true, now}` (clock back to Monday, memory wiped, warmup re-run) |
| GET | `/review/pending` | | `[{id, user, query, reason, created_at}]` (hr only) |
| POST | `/review/{id}` | `?approve=true&transform=T3_widen_scope` | `{ok: true}` (hr only) |

### Demo controls

All `/admin/clock`, `/admin/simulate-update` and `/admin/reset` calls require a known
`X-Demo-User`. These rehearsal controls are available to every demo user; policy draft
and activation endpoints require the `hr` role. Policy activation records the resolved
user as the approver, never a client-supplied name.

The clock starts at Monday **2026-11-09 09:00 UTC**. `days` adds to the current offset
and must be finite, positive, at least 1 ms, and within the supported date range.
Advance the clock before modifying employee data; updates at Monday's starting instant
return 409. Values must match the stored field type (integer fields reject fractional
values; numeric values reject NaN/Infinity; Attrition accepts only `Yes` or `No`).
An update and its audit event commit together with the same simulated timestamp.

Reset requires the locally cached `backend/data/ibm_hr.csv` from `scripts/load_data.py`
and an active `ibm_hr` policy. It restores changed employees without dropping the
collection, clears releases/precedents/review approvals, deactivates the kill switch,
and creates exactly two Monday warmup releases for `comp-review-2026`: `mgr_1` / sales
and `mgr_4` / hr. It preserves the append-only audit log and other control documents.
All reset database changes commit together or roll back together. The HTTP handler
never downloads a missing baseline.

Errors: 401 missing/unknown user; 403 wrong role; 404 missing employee/policy version;
400 or 422 invalid input; 409 invalid demo state; 503 missing policy/baseline/employee
data. Database failures return a server error and must not be treated as success.

`dev:mock` starts with the same Monday warmup and scripts Act 1/Act 2 only. Its update
control supports **employee 1408 / MonthlyIncome / 18500**; other edits return an error.
It models the history-ambiguity check for these scenarios but is not the real gate.

## ChatResponse

```json
{
  "reply": "text the LLM wrote",
  "trace": [ TraceEntry, ... ]          // one per tool call, in order
}
```

### TraceEntry (one `query_hr` call)

```json
{
  "tool": "query_hr",
  "memory_on": true,
  "query": {"filters": [{"col": "Department", "op": "=", "val": "Sales"}],
            "group_by": [], "metric": "avg", "target": "MonthlyIncome",
            "transform": null},
  "status": "allowed | partial | blocked | error",
  "rule": "L4.pairwise",                // null when allowed
  "evidence_text": "Since your last query on Mon 11/09 ...",   // null when allowed
  "cells": [{"group": {}, "n": 36, "value": 17111.5}],         // never employee ids
  "suppressed": [{"group": {"Gender": "Female"}, "reason": "fewer than 5 people"}],
  "allowed_transforms": ["T3_widen_scope"],
  "hints": ["Reviewer precedent: widen to the whole department"]
}
```

`request_review` calls appear as `{"tool": "request_review", "approval_id": "...", "status": "pending"}`.
