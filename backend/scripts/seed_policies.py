"""Manual policy for the IBM HR dataset.

Writes a DRAFT via app.policy.store.create_draft(..., drafted_by="manual"),
then activates it via store.activate(..., approved_by=<name>).
Same path the agent-drafted policy (app/policy/profiler.py) will use.
Re-running creates a NEW version and retires the previous one (all in the ledger).

The permissions are defined in app/policy/defaults.py and docs/collections.md. Default deny: a column missing from FIELD_LABELS cannot be queried at all.

Run from backend/:
  python scripts/seed_policies.py --approved-by "<your name>"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `app` importable

from app.policy.defaults import DATASET, FIELD_LABELS, POLICY  # noqa: E402


def check_labels_cover_data(db) -> None:
    """Fail early if the policy and the loaded data disagree on column names."""
    sample = db.employees.find_one({}, {"_id": 0, "active": 0, "start_date": 0, "updated_at": 0})
    if sample is None:
        sys.exit("employees is empty: run scripts/load_data.py first")
    data_cols, labelled = set(sample), set(FIELD_LABELS)
    if data_cols != labelled:
        sys.exit(f"label mismatch. unlabelled: {sorted(data_cols - labelled)}; "
                 f"not in data: {sorted(labelled - data_cols)}")


if __name__ == "__main__":
    from app.db import ensure_indexes, get_db
    from app.policy import store

    ap = argparse.ArgumentParser()
    ap.add_argument("--approved-by", required=True, help="the human approving this policy")
    args = ap.parse_args()

    db = get_db()
    check_labels_cover_data(db)
    ensure_indexes(db)
    store.ensure_policy_indexes(db)
    version = store.create_draft(db, DATASET, POLICY, drafted_by="manual")
    store.activate(db, DATASET, version, approved_by=args.approved_by)
    active = store.load_active(db, DATASET)
    print(f"active policy: {active['_id']} (approved by {active['approved_by']}), "
          f"{len(active['field_labels'])} labelled fields")
