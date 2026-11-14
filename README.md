# Memory-Governed Privacy Agent for HR Analytics

An HR analytics AI assistant with a **memory-governed privacy gate**: before any statistic
leaves the system, the gate checks it against a durable, cross-session ledger of what each
user has already been told, and blocks answers that could be combined with earlier ones to
reveal an individual (e.g. a colleague's salary, or an "anonymous" survey response).

Track: **Memory You Can Govern** (MongoDB Atlas + Vector Search).

> The agreed structure, interfaces and TODOs.

## How a request flows

1. Manager asks a question in plain language (`POST /chat`).
2. The LLM fills a **structured query** (JSON, whitelisted fields) and calls the `query_hr` tool.
3. `query/executor.py` turns the JSON into an Atlas aggregation and runs it; results stay on the server.
4. `gate/check()` decides: **allowed / partial / blocked**, using the user's release history.
   The gate is deterministic Python: no LLM in the decision.
   The cross-query check counts both people who joined or left a population and people
   whose sensitive value changed since the earlier answer (e.g. a raise).
5. Allowed cells are **written to the release ledger first, then returned** (write-before-release).
6. If blocked, the LLM gets the evidence plus allowed rewrites, retries (max 3), or calls `request_review`.
7. Reviewer approvals become precedents (vector-indexed) that guide the next similar request.

## Layout and owners

```
backend/
  app/
    main.py              FastAPI routes                         (Clarice)
    config.py            settings from env                      (Clarice)
    db.py                Atlas client, collections, indexes     (Clarice)
    schemas.py           shared data contracts  ← agree Friday  (all)
    clock.py             simulated clock for demo               (Clarice)
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
python scripts/seed_policies.py
uvicorn app.main:app --reload

cd ../frontend
npm install && npm run dev
```

Atlas Vector Search index on `precedents.embedding` is created in the Atlas UI
(definition in `app/db.py`).

## Data

IBM HR Analytics Employee Attrition & Performance: a **fictional** dataset of 1,470 employees
created by IBM data scientists as Watson Analytics sample data. License: ODbL / DbCL, as stated
in IBM's `employee-attrition-aif360` repository. Downloaded from OpenML (data id 43896).
No real person's data is used.

## Prior work we build on

- Disclosure-control rules (threshold, dominance, differencing) as used by Statistics Canada,
  ABS and ONS output checking; Hundepool et al., *Handbook on Statistical Disclosure Control*.
- ACRO (AI-SDC, MIT) for single-output checks; query auditing (Chin & Özsoyoglu, 1982).
- What is new here: running these checks at agent speed, a governed cross-session disclosure
  memory in Atlas, and an agent that recovers from a block instead of just refusing.
