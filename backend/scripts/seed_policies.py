"""Manual policy for the IBM HR dataset (Suey + Wenqian decide the values on Friday).

Writes a DRAFT via app.policy.store.create_draft(..., drafted_by="manual"),
then activates it via store.activate(..., approved_by="<name>").
Same path the agent-drafted policy (app/policy/profiler.py) will use.

Contents:
- dataset name
- roles → allowed targets and metrics
- sensitive targets, quasi-identifiers, never-returnable fields
- k_threshold, median_threshold, dominance share
- memory scope and retention
"""

if __name__ == "__main__":
    raise NotImplementedError
