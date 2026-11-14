# Atlas collections

Database: `privacy_agent`. Six collections. Validators are `$jsonSchema` with
`validationAction: "error"`.

Two design rules apply throughout:

- **Data minimisation.** Memory stores *who has seen which population*, never the released
  numbers. `releases` holds ids and counts; values live only in `employees`.
- **Defense in depth.** The `releases` validator rejects any record with `n < 5`. Because the
  gate writes the release *before* returning a result, even a buggy gate cannot hand out a
  small cell: the write fails, so nothing is released.

---

## employees

IBM HR Analytics, one document per employee, all 35 original columns as-is, plus:

| Field | Type | Notes |
| --- | --- | --- |
| `EmployeeNumber` | int | unique id used in every population set |
| `active` | bool | `false` = has left the company |
| `start_date` | date | first day of employment; executor matches `active: true` **and** `start_date <= clock.now()`, so advancing the demo clock makes a new hire appear |
| `AgeBand` | string | e.g. `"25-29"`; derived from `Age` and inherits Sensitive Basic Information authorization |
| `updated_at` | object | last-modified time **per statistical target**, e.g. `{"MonthlyIncome": date, "JobSatisfaction": date}`; `{}` at load |

Indexes: `{EmployeeNumber: 1}` unique; `{Department: 1, JobLevel: 1, Gender: 1}`.
No validator (loaded once by `scripts/load_data.py`).

**`updated_at`** lets the gate detect value changes (type B in L4): a person who stays in a
population but whose salary changed since an earlier answer. Every numeric statistical target in policy `tracked_targets` is tracked, including
Other Basic Information such as `YearsAtCompany`; a missing key means "not modified
since load". The list is derived from fields offering `avg`, `sum`, or `median`,
independently of business categories and privacy labels.

- `EmployeeNumber` is a lookup selector in individual mode, never a projected field or aggregate input; it is not a tracked value.
- A categorical grouping field such as `Department` needs no value timestamp: moving
  employees between populations is detected by the id-set comparison (type A). A field
  that can also be a numeric target, such as `JobLevel`, is tracked for value changes
  even when it is also used to filter or group queries.

Maintenance: in a real deployment, an Atlas change stream or Atlas Trigger sets
`updated_at.<field>` whenever a field in `tracked_targets` is modified. In the demo,
`POST /admin/simulate-update` writes the value and `updated_at.<field>` (simulated clock).

---

## policies

One document per policy version and dataset. Lifecycle `draft → active → retired`;
exactly one `active` per dataset. The source configuration is
`backend/app/policy/defaults.py`; the seed script creates and activates a new version.
Existing version 1 policies must be replaced by a reviewed version 2 policy before
serving queries. Old `targets`/`metrics` role grants are not interpreted as version 2.
The validator permits legacy documents only in `retired` status, so activating a new
version 2 policy can retire the existing version 1 policy in the same transaction
without bypassing validation. These legacy records remain available for audit only;
the policy store rejects legacy creation, activation, reactivation, and active-policy
reads. A retired version 2 policy can still be reactivated for rollback.

Version 2 separates business authorization from statistical release checks:

- `schema_version: 2` identifies the contract.
- `field_categories` maps every returnable field to one of the five categories below.
- `roles[role]` has a server-enforced `scope` and a `grants` map, whose category values
  are arrays containing `individual`, `aggre_stats`, both, or neither.
- `tracked_targets` lists every field with a numeric statistical metric, so timestamp
  tracking protects both sensitive fields and Other Basic Information.
- `field_metrics` lists applicable metrics for each field. Counts use approved
  filters/grouping fields; strings cannot be numeric targets. Numeric fields can
  support mean (`avg`) and median. Sums are offered only for the
  additive fields listed in the configuration; ordinal ratings are not summed.
- `field_labels`, `quasi_identifiers`, and `never_return` govern privacy and allowed
  query construction independently of the category grants.
- `k_threshold >= 5` governs statistical cells; `median_threshold` defaults to 10.
  Gate also enforces an unlowerable five-person minimum. Authorization does not
  promise release: homogeneity, complementary suppression and history checks remain.
  Median uses an unlowerable ten-person floor and exact computation. A median or
  sensitive categorical count connected to earlier disclosures is blocked until a
  suitable nonlinear privacy check exists; it is never treated as a sum equation.

| Role key | Scope | Other Basic | Sensitive Basic | Salary | Measurement Report | Performance Rating |
| --- | --- | --- | --- | --- | --- | --- |
| `ceo` | Company | A | — | A | A | A |
| `vp` | Assigned department | A | — | A | A | A |
| `hr_head` | Company | I + A | I + A | I + A | I + A | I + A |
| `hr_salary_manager` | Company | I + A | I + A | I + A | A | A |
| `department_hr_manager` | Assigned department | I + A | I + A | A | I + A | I + A |
| `department_manager` | Assigned department | A | — | A | A | I + A |
| `ordinary_hr` | Own HR department | I | I | — | — | A |
| `employee` | None | — | — | — | — | — |

I means `individual`; A means `aggre_stats`. They are independent grants: Individual
only does not imply aggregate access. Conversely, an Individual grant never exempts
an aggregate query from the minimum cohort size or other privacy checks.

The concrete IBM HR mapping is:

| Category | Fields |
| --- | --- |
| `other_basic` | `Attrition`, `BusinessTravel`, `Department`, `Education`, `EducationField`, `EmployeeCount`, `Gender`, `JobLevel`, `JobRole`, `NumCompaniesWorked`, `OverTime`, `StandardHours`, `TotalWorkingYears`, `TrainingTimesLastYear`, `YearsAtCompany`, `YearsInCurrentRole`, `YearsSinceLastPromotion`, `YearsWithCurrManager` |
| `sensitive_basic` | `Age`, `DistanceFromHome`, `MaritalStatus`, `Over18`, `StockOptionLevel`; also derived `AgeBand` |
| `salary` | `DailyRate`, `HourlyRate`, `MonthlyIncome`, `MonthlyRate`, `PercentSalaryHike` |
| `measurement_report` | `EnvironmentSatisfaction`, `JobInvolvement`, `JobSatisfaction`, `RelationshipSatisfaction`, `WorkLifeBalance` |
| `performance_rating` | `PerformanceRating` |

`EmployeeNumber` is an identifier, not a returnable business field. It may select
employees only in the explicit Individual query. `StockOptionLevel` does not inherit
Salary grants, and `PerformanceRating` does not inherit Measurement Report grants.
The dataset's `Attrition` is classified as employment-status basic information;
Measurement Report denotes the five survey/measurement fields listed above.

The server directory supplies `Identity.departments` using the actual dataset
Department values. `team` continues to define disclosure-history sharing and is not
a data-access scope. Every executor query intersects the user's filters with the
server-authorized departments. A rewrite may widen user filters only within that
scope. Ordinary HR must be assigned to an approved HR department.

Individual queries explicitly select employee numbers and projected fields. Their
result and audit path are separate from the aggregate release ledger; they do not
create fictitious one-person aggregate releases or weaken its `n >= 5` validator.
Only authorized projected fields reach the agent. Aggregate results reach the agent
only after the Gate's public-result conversion and required audit/release writes.
Each Individual result is `{request_index, values}`: the zero-based index refers to
the input employee list, so unordered or partial database results remain unambiguous
without returning employee identifiers. Duplicate input employee numbers are rejected.

Validator requirements include `dataset`, `version`, `status`, `drafted_by`,
`schema_version`, `k_threshold`, `field_categories`, `field_metrics`, `field_labels`,
`quasi_identifiers`, `never_return`, `sensitive_targets`, `tracked_targets`, and all
eight role grants.
`approved_by` is required for an active policy. Unrecognized role, category, scope,
or grant values are rejected; missing authorization information fails closed.

---

## releases — security memory (disclosure ledger)

One document per **released cell**. Complete and lossless; never summarised.

| Field | Type | Notes |
| --- | --- | --- |
| `user` | string | who received it |
| `team` | string | for team-scoped checks |
| `project` | string | memory isolation boundary |
| `session_id` | string | which conversation (for audit; not used by the gate) |
| `target` | string | sensitive field, e.g. `MonthlyIncome` |
| `metric` | string | `count` / `sum` / `avg` / `median` |
| `cell_key` | object | group values, e.g. `{"Gender": "Female"}` |
| `label` | string | fixed-format description for evidence text, e.g. `"Sales / Manager, average MonthlyIncome"`; built from the query, never contains values |
| `ids` | array<int> | population S at release time (snapshot) |
| `n` | int | `len(ids)`; validator: **≥ 5** |
| `policy_version` | int | rules in force |
| `created_at` | date | release time; also the baseline for B-type changes (see below) |
| `expires_at` | date | TTL; set from `policy.memory.retention_days` |

`created_at` doubles as the baseline for value changes: for a new cell compared with this
release, an employee in both populations counts as changed (type B) when
`employees.updated_at[target] > created_at`. No separate time-window parameter is needed.
It is written with the simulated clock, the same clock that sets `updated_at` in the demo.

```json
{
  "$jsonSchema": {
    "bsonType": "object",
    "required": ["user", "team", "project", "target", "metric", "cell_key", "label",
                 "ids", "n", "policy_version", "created_at", "expires_at"],
    "properties": {
      "user": {"bsonType": "string"},
      "team": {"bsonType": "string"},
      "project": {"bsonType": "string"},
      "session_id": {"bsonType": "string"},
      "target": {"bsonType": "string"},
      "metric": {"enum": ["count", "sum", "avg", "median"]},
      "cell_key": {"bsonType": "object"},
      "label": {"bsonType": "string"},
      "ids": {"bsonType": "array", "items": {"bsonType": "int"}, "minItems": 5},
      "n": {"bsonType": "int", "minimum": 5},
      "policy_version": {"bsonType": "int"},
      "created_at": {"bsonType": "date"},
      "expires_at": {"bsonType": "date"}
    },
    "additionalProperties": true
  }
}
```

Indexes: `{project: 1, target: 1, user: 1}`, `{project: 1, target: 1, team: 1}`,
TTL `{expires_at: 1}` with `expireAfterSeconds: 0`.

Access: written only by `memory.releases.record_then_release` inside a transaction;
read only by the gate. No API returns its contents. Users cannot delete their own records.

---

## precedents — semantic memory

| Field | Type | Notes |
| --- | --- | --- |
| `project` | string | Vector Search filter |
| `request_text` | string | normalised description of the blocked request |
| `embedding` | array<double> | Voyage embedding of `request_text` |
| `transform` | string | `T1_merge_groups` / `T2_drop_filter` / `T3_widen_scope` |
| `decision` | string | `approved` / `rejected` |
| `reviewer` | string | |
| `created_at` | date | |
| `overturned` | bool | correction flag; Vector Search filter |
| `overturned_by` / `overturn_reason` / `replaces` | string | correction history |

Validator (required): `project`, `request_text`, `embedding`, `transform` (enum),
`decision` (enum), `reviewer`, `created_at`, `overturned`.
Vector Search index: see `backend/app/db.py`.

---

## ledger — audit log (append-only)

One document per event. Never stores released values or new data values.
Three kinds:

| `kind` | Written by | Extra required fields |
| --- | --- | --- |
| `gate_decision` | `agent.tools.handle_query_hr` | `query`, `status`, `policy_version` |
| `data_update` | `POST /admin/simulate-update` (real deployment: the Atlas Trigger) | `employee_number`, `field` |
| `policy_change` | `policy.store.activate` | `dataset`, `from_version`, `to_version` |

Fields of a `gate_decision` entry:

| Field | Type |
| --- | --- |
| `kind` | `gate_decision` / `data_update` / `policy_change` |
| `time` | date |
| `user`, `role`, `team`, `project`, `session_id` | string |
| `memory_on` | bool (false for the control panel) |
| `query` | object (the StructuredQuery) |
| `status` | `allowed` / `partial` / `blocked` / `error` |
| `rule` | string, e.g. `L4.pairwise` |
| `evidence_text` | string (from `gate.evidence`) |
| `suppressed` | array of `{cell_key, reason}` |
| `rewrite_of` | id of the ledger entry this retry rewrites, if any |
| `approval_id` | string, if a review was requested |
| `policy_version` | int |

Validator: always required `kind` (enum), `time`, `user`; then per kind (via `oneOf` on
`kind`) the extra fields in the table above. `status` is an enum for `gate_decision`.
Append-only: the app's database user gets a custom role with only `insert` and `find`
on `ledger`.

---

## approvals — review requests and kill switch

```json
{"_id": "...", "kind": "review", "project": "comp-review-2026", "user": "mgr_1",
 "query": {...}, "reason": "...", "status": "pending",
 "transform": null, "reviewer": null, "created_at": {...}, "decided_at": null}

{"_id": "kill_switch", "kind": "kill_switch", "active": false}
```

Validator (required): `kind` (`review` / `kill_switch`); for reviews also `status`
(`pending` / `approved` / `rejected`). The backend watches this collection with a change stream.

---

## meta — demo clock

One document: `{"_id": "clock", "offset_days": 3.0}`. `app/clock.py` reads it so the API
server and `scripts/warmup.py` (separate processes) agree on the simulated "now"
(`DEMO_START` = Mon 2026-11-09 09:00 UTC + offset). Reset to 0 by `/admin/reset`.

`policies` indexes: `{dataset: 1, version: 1}` unique, and `{dataset: 1}` unique with
`partialFilterExpression: {status: "active"}` so the database itself allows only one active
version per dataset (created by `store.ensure_policy_indexes`).
