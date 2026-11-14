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
