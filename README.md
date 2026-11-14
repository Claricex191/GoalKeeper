# Memory-Governed Privacy Agent for HR Analytics

An HR analytics AI assistant with a **memory-governed privacy gate**: before any statistic
leaves the system, the gate checks it against a durable, cross-session ledger of what each
user has already been told, and blocks answers that could be combined with earlier ones to
reveal an individual (e.g. a colleague's salary, or an "anonymous" survey response).

Track: **Memory You Can Govern** (MongoDB Atlas + Vector Search).

> The agreed structure, interfaces and TODOs.

## How a request flows

1. Manager asks a question in plain language (`POST /chat`). The client sends only a user id
   (`X-Demo-User` header); role and team come from the server-side directory in
   `app/users.py`, never from the request. (Real deployment: SSO / JWT instead.)
2. The LLM fills a **structured query** (JSON, whitelisted fields) and calls the `query_hr` tool.
   Gate checks the server-resolved identity against the eight-role permission matrix
   before employee data is read. Individual and `aggre_stats` are independent grants.
3. `query/executor.py` intersects every query with the server-assigned departments.
   Aggregate cells and explicitly projected Individual records stay on the server.
4. `gate/check()` decides: **allowed / partial / blocked**, using the user's release history.
   The gate is deterministic Python: no LLM in the decision.
   The cross-query check counts both people who joined or left a population and people
   whose sensitive value changed since the earlier answer (e.g. a raise).
5. Allowed cells are **written to the release ledger first, then returned** (write-before-release).
6. If blocked, the LLM gets the evidence plus allowed rewrites, retries (max 3), or calls `request_review`.
7. Reviewer approvals become precedents (vector-indexed) that guide the next similar request.

The implemented query handler enforces authorization, scope and write-before-release.
The surrounding `/chat` LLM loop and review endpoints remain TODOs. The full role and
field mapping is in [the policy specification](docs/collections.md#policies), with its
source in `backend/app/policy/defaults.py`. Existing v1 policies must be replaced with
a v2 policy before queries are served; the new code denies legacy flat grants.

## Layout and owners

```
backend/
  app/
    main.py              FastAPI routes                         (Clarice)
    config.py            settings from env                      (Clarice)
    db.py                Atlas client, collections, indexes     (Clarice)
    schemas.py           shared data contracts  ← agree Friday  (all)
    clock.py             simulated clock for demo               (Clarice)
    users.py             demo user directory: id → role, team   (Clarice)
    agent/
      loop.py            LLM tool-calling loop                  (Clarice)
      prompts.py         system prompt                          (Clarice + Qi Jia)
      tools.py           query_hr / request_review              (Clarice)
    query/
      executor.py        JSON → aggregation → cells             (Clarice)
    gate/
      __init__.py        check() = L1 → L4                      (Wenqian)
      rules.py           L1 field policy, L2 cell rules, L3     (Wenqian)
      cross_query.py     L4 pairwise + linear algebra           (Wenqian)
      evidence.py        human-readable reasons (templates)     (Wenqian)
    memory/
      releases.py        disclosure ledger (security memory)    (Clarice)
      precedents.py      reviewer precedents + Vector Search    (Clarice)
      ledger.py          append-only audit log                  (Clarice)
    review/
      approvals.py       approval requests + change streams     (Clarice)
    policy/
      store.py           policy versions: draft → active (human) (Clarice)
      profiler.py        STRETCH: agent drafts a policy for a new dataset
  scripts/
    load_data.py         IBM HR → Atlas                         (Clarice)
    seed_policies.py     roles, sensitive fields, thresholds    (Suey)
    warmup.py            replay "last week" through the real pipeline (Qi Jia)
  tests/
    test_gate.py         gate unit tests from case files        (Wenqian)
    cases/               demo questions + attack scripts (JSON) (Qi Jia, Suey)
frontend/                Vite + React                           (Clarice)
docs/
  collections.md         Atlas collections, fields, validators, indexes
```

## Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Python 3.11 or 3.12
pip install -r requirements.txt
cp .env.example .env                                 # fill in keys
python scripts/load_data.py
python scripts/seed_policies.py --approved-by "<your name>"
uvicorn app.main:app --reload

cd ../frontend
npm install && npm run dev
```

Atlas Vector Search index on `precedents.embedding` is created in the Atlas UI
(definition in `app/db.py`).

### Your own `.env` (keys and connection string)

Each member keeps their own `backend/.env`. It is listed in `.gitignore`, so it is never
committed or pushed; only `backend/.env.example` (placeholders, no secrets) is in the repo.

1. `cd backend && cp .env.example .env`
2. In Atlas: **Database Access** → create a database user for yourself;
   **Network Access** → add your current IP address.
3. **Connect → Drivers → Python**: copy the `mongodb+srv://...` string, put in your
   username and password, and paste it as `MONGODB_URI=` in `.env`.
4. Fill in `LLM_API_KEY`, `LLM_MODEL` and `VOYAGE_API_KEY` as needed.
5. Run scripts and `uvicorn` from inside `backend/`, so `.env` is found.

Before committing, `git status` should never list `.env`. If a key is ever pushed by
mistake, rotate it (create a new one); deleting the commit is not enough.

## Data

IBM HR Analytics Employee Attrition & Performance: a **fictional** dataset of 1,470 employees
created by IBM data scientists as Watson Analytics sample data. License: ODbL / DbCL, as stated
in IBM's `employee-attrition-aif360` repository. The loader can download from OpenML
(data id 43896); the source of the CSV used for validation is recorded below.
No real person's data is used.

### Validate with the complete CSV

From `backend/`, install `requirements-test.txt`, then run:

```bash
python scripts/load_data.py --dry-run
python -m pytest -q tests/test_gate.py tests/test_ibm_csv_permissions.py
```

The CSV tests require all 1,470 rows and 35 original fields. They run the actual
loader, query executor and Gate against a local `mongomock` database, and compare
statistics and employee populations with an independent CSV calculation. This does
not replace the separate Atlas integration tests. The local CSV is ignored by Git;
`backend/data/ibm_hr.source.json` records the pinned IBM source and SHA-256 of the
file used for validation.

## Prior work we build on

- Disclosure-control rules (threshold, dominance, differencing) as used by Statistics Canada,
  ABS and ONS output checking; Hundepool et al., *Handbook on Statistical Disclosure Control*.
- ACRO (AI-SDC, MIT) for single-output checks; query auditing (Chin & Özsoyoglu, 1982).
- What is new here: running these checks at agent speed, a governed cross-session disclosure
  memory in Atlas, and an agent that recovers from a block instead of just refusing.
