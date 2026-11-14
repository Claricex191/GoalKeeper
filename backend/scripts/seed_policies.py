"""Manual policy for the IBM HR dataset.

Writes a DRAFT via app.policy.store.create_draft(..., drafted_by="manual"),
then activates it via store.activate(..., approved_by=<name>).
Same path the agent-drafted policy (app/policy/profiler.py) will use.
Re-running creates a NEW version and retires the previous one (all in the ledger).

Values below are the starting point from docs/collections.md; Suey + Wenqian adjust
them here. Default deny: a column missing from FIELD_LABELS cannot be queried at all.

Run from backend/:
  python scripts/seed_policies.py --approved-by "<your name>"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `app` importable

from app.db import ensure_indexes, get_db  # noqa: E402
from app.policy import store  # noqa: E402

DATASET = "ibm_hr"

SENSITIVE = ["MonthlyIncome", "PerformanceRating", "Attrition", "JobSatisfaction"]
QUASI = ["Department", "JobRole", "JobLevel", "Gender", "AgeBand", "YearsAtCompany"]
NEVER_RETURN = ["EmployeeNumber", "EmployeeCount", "Over18", "StandardHours"]

# Every column in `employees` gets a label (system fields active/start_date/updated_at excluded).
FIELD_LABELS = {
    "EmployeeNumber": "identifier",
    **{c: "sensitive" for c in SENSITIVE},
    **{c: "quasi_identifier" for c in QUASI},
    # pay-related and satisfaction fields: sensitive, but not offered as query targets yet
    **{c: "sensitive" for c in ["DailyRate", "HourlyRate", "MonthlyRate", "PercentSalaryHike",
                                "StockOptionLevel", "EnvironmentSatisfaction",
                                "RelationshipSatisfaction", "WorkLifeBalance",
                                "JobInvolvement"]},
    # everything else: not queryable as a filter / group / target in v1
    **{c: "other" for c in ["Age", "BusinessTravel", "DistanceFromHome", "Education",
                            "EducationField", "EmployeeCount", "MaritalStatus",
                            "NumCompaniesWorked", "Over18", "OverTime", "StandardHours",
                            "TotalWorkingYears", "TrainingTimesLastYear",
                            "YearsInCurrentRole", "YearsSinceLastPromotion",
                            "YearsWithCurrManager"]},
}

LABEL_REASONS = {
    "EmployeeNumber": "unique per employee",
    "MonthlyIncome": "pay data",
    "PerformanceRating": "performance review outcome",
    "Attrition": "whether the person left",
    "JobSatisfaction": "survey answer, promised anonymous",
}

POLICY = {
    "field_labels": FIELD_LABELS,
    "label_reasons": LABEL_REASONS,
    "k_threshold": 5,
    "median_threshold": 10,
    "dominance_share": 0.6,
    "sensitive_targets": SENSITIVE,
    "quasi_identifiers": QUASI,
    "never_return": NEVER_RETURN,
    "roles": {
        "manager":  {"targets": ["MonthlyIncome", "JobSatisfaction"], "metrics": ["count", "avg"]},
        "hr":       {"targets": ["*"], "metrics": ["count", "sum", "avg"]},
        "employee": {"targets": [], "metrics": ["count"]},
    },
    # Retention = how long a release still helps someone piece together a person.
    # Pay stays linkable for years; survey answers roughly one survey cycle.
    # PLACEHOLDER values: Suey + Qi Jia decide.
    "memory": {
        "scope": "team",                    # user | team | org
        "retention_days": 365,              # default for targets not listed below
        "retention_by_target": {"MonthlyIncome": 730, "PerformanceRating": 730},
    },
}


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
