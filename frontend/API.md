# Frontend ↔ backend contract

The browser calls `/api/...`; Vite proxies it to `http://localhost:8000/...`.
Every call sends `X-Demo-User: <user id>`. The backend resolves role, memory team and
authorized departments itself. The browser cannot supply permissions or data scope.

## Endpoints

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| GET | `/demo/users` | | `[{user, role, team, display_name, departments}]` |
| POST | `/chat` | `{project, session_id, message, memory}` | `ChatResponse` (below) |
| GET | `/admin/clock` | | `{now: ISO string, offset_days: number}` |
| POST | `/admin/clock` | `?days=3` | `{now, offset_days}` (hr_head only) |
| POST | `/admin/simulate-update` | `?employee_number=1408&field=MonthlyIncome&new_value=18500` | `{ok: true}` (hr_head only) |
| POST | `/admin/reset` | | `{ok: true, now}` (hr_head only; clock back to Monday, memory wiped, warmup re-run) |
| GET | `/review/pending` | | `[{id, user, query, reason, created_at}]` (hr_head only) |
| POST | `/review/{id}` | `?approve=true&transform=T3_widen_scope` | `{ok: true}` (hr_head only) |

### Demo controls

Every management call requires a known `X-Demo-User`. All known users can read
`GET /admin/clock`; only the `hr_head` role (`hr_1`, HR Head) may advance the clock,
simulate an update, or reset the demo. Other roles receive 403, and the UI disables
these controls with a reminder to switch to HR Head. Policy draft and activation
endpoints also require `hr_head`. Policy activation records the resolved user as
the approver, never a client-supplied name.

The clock starts at Monday **2026-11-09 09:00 UTC**. `days` adds to the current offset
and must be finite, positive, at least 1 ms, and within the supported date range.
Advance the clock before modifying employee data; updates at Monday's starting instant
return 409. The updated field must belong to the active policy's `tracked_targets`
whitelist. This covers numerical aggregate targets such as `MonthlyIncome`,
`JobSatisfaction`, and `PerformanceRating`; categorical `Attrition` is not eligible.
Values must match the stored field type: integer fields reject fractional values,
and numeric values reject NaN/Infinity. An update and its audit event commit together
with the same simulated timestamp.
Changing `Age` also updates the derived `AgeBand` in that transaction; a changed
band receives its own field timestamp for history checks.

Reset requires the locally cached `backend/data/ibm_hr.csv` from `scripts/load_data.py`
and an active v2 `ibm_hr` policy. It restores changed employees without dropping the
collection, clears releases/precedents/review approvals, deactivates the kill switch,
and creates exactly two Monday warmup releases for `comp-review-2026`: `mgr_1` / sales
and `mgr_4` / hr. It preserves the append-only audit log and other control documents.
All reset database changes commit together or roll back together. The HTTP handler
never downloads a missing baseline.
The retained ledger gets a new L5 counting boundary, and a successful API reset
clears this server's chat context so previous demo runs do not carry forward.

Errors: 401 missing/unknown user; 403 wrong role; 404 missing employee/policy version;
400 or 422 invalid input; 409 invalid demo state; 503 missing policy/baseline/employee
data. Database failures return a server error and must not be treated as success.

`dev:mock` starts with the same Monday warmup and scripts Act 1/Act 2 only. Its update
control supports **employee 1408 / MonthlyIncome / 18500**; other edits return an error.
It models department authorization, salary aggregate grants, and the history-ambiguity
check for these scenarios, but is not the real gate. Turning memory off never bypasses
authorization: a department manager can query only their assigned department.

To rehearse both scenarios:

1. Select **HR Head**, reset to Monday, advance two days to Wednesday, apply employee
   1408's `MonthlyIncome = 18500`, then advance one day to Thursday.
2. Select **Manager 1 (Sales)** and ask for Sales managers' average monthly income.
3. Select **Manager 4 (HR dept.)** and ask for HR managers' average monthly income.

Both memory-on queries should block with `L4.pairwise`; memory-off still applies
role/department checks and the single-query privacy rules. Switching users starts
fresh chat panels while the server's team disclosure history remains available.

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
  "query": {"mode": "aggre_stats", "filters": [{"col": "Department", "op": "=", "val": "Sales"}],
            "group_by": [], "metric": "avg", "target": "MonthlyIncome",
            "transform": null},
  "status": "allowed | partial | blocked | error",
  "rule": "L4.pairwise",                // null when allowed
  "evidence_text": "Since your last query on Mon 11/09 ...",   // null when allowed
  "cells": [{"group": {}, "n": 36, "value": 17111.5}],         // never employee ids
  "suppressed": [{"group": {"Gender": "Female"}, "reason": "hidden to protect privacy"}],
  "allowed_transforms": ["T3_widen_scope"],
  "hints": ["Reviewer precedent: widen to the whole department"]
}
```

L5 escalations remain `query_hr` entries with `status: "blocked"`,
`rule: "L5.probing"` and `approval_id`. Later requests awaiting the same review have
`rule: "L5.pending"`. Reviewer decisions reset probing; each query still passes
the current authorization and privacy checks.

### Individual reads

Roles with Individual grants also receive the `query_hr_individual` tool. Its trace uses the
same status/evidence fields, empty `cells`, and these additional values:

```json
{
  "tool": "query_hr_individual",
  "query": {"mode": "individual", "fields": ["JobRole"], "requested_count": 2},
  "records": [{"request_index": 0, "values": {"JobRole": "Manager"}}]
}
```

`request_index` maps to the zero-based position in the tool's requested employee
list. The public trace omits employee selectors; absent or out-of-scope employees
are omitted from records. Individual values retain their original types and precision.
Aggregate cell values are rounded to two decimal places. Tool handlers return
`{for_llm, trace}`; only `for_llm` is appended to the model's conversation.
