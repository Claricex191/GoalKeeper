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
| POST | `/admin/clock` | `?days=3` | `{now, offset_days}` |
| POST | `/admin/simulate-update` | `?employee_number=1408&field=MonthlyIncome&new_value=18500` | `{ok: true}` |
| POST | `/admin/reset` | | `{ok: true, now}` (clock back to Monday, memory wiped, warmup re-run) |
| GET | `/review/pending` | | `[{id, user, query, reason, created_at}]` (hr_head only) |
| POST | `/review/{id}` | `?approve=true&transform=T3_widen_scope` | `{ok: true}` (hr_head only) |

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
