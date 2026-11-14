"""Version 2 permissions for the IBM HR dataset; safe to import without a database.

Business categories decide authorization. Privacy labels and statistical release
rules remain separate: an aggre_stats grant does not promise a released result.
The full field mapping and matrix are documented in docs/collections.md.
"""

DATASET = "ibm_hr"
CATEGORIES = ("other_basic", "sensitive_basic", "salary", "measurement_report",
              "performance_rating")
DEPARTMENTS = ["Sales", "Research & Development", "Human Resources"]

CATEGORY_FIELDS = {
    "other_basic": [
        "Attrition", "BusinessTravel", "Department", "Education", "EducationField",
        "EmployeeCount", "Gender", "JobLevel", "JobRole", "NumCompaniesWorked",
        "OverTime", "StandardHours", "TotalWorkingYears", "TrainingTimesLastYear",
        "YearsAtCompany", "YearsInCurrentRole", "YearsSinceLastPromotion",
        "YearsWithCurrManager",
    ],
    "sensitive_basic": ["Age", "AgeBand", "DistanceFromHome", "MaritalStatus", "Over18",
                        "StockOptionLevel"],
    "salary": ["DailyRate", "HourlyRate", "MonthlyIncome", "MonthlyRate", "PercentSalaryHike"],
    "measurement_report": ["EnvironmentSatisfaction", "JobInvolvement", "JobSatisfaction",
                           "RelationshipSatisfaction", "WorkLifeBalance"],
    "performance_rating": ["PerformanceRating"],
}
FIELD_CATEGORIES = {name: category for category, names in CATEGORY_FIELDS.items()
                    for name in names}
QUASI = ["Department", "JobRole", "JobLevel", "Gender", "AgeBand", "YearsAtCompany"]
NEVER_RETURN = ["EmployeeNumber"]
SENSITIVE = [name for name, category in FIELD_CATEGORIES.items() if category != "other_basic"]
FIELD_LABELS = {
    **{name: "other" for name in FIELD_CATEGORIES},
    **{name: "sensitive" for name in SENSITIVE},
    **{name: "quasi_identifier" for name in QUASI if name not in SENSITIVE},
    "EmployeeNumber": "identifier",
}
LABEL_REASONS = {
    "EmployeeNumber": "unique per employee; lookup selector only in individual mode",
    "AgeBand": "derived from Age; inherits sensitive basic authorization",
    "StockOptionLevel": "sensitive basic information, independent of salary grants",
    "PerformanceRating": "separate performance rating authorization",
}

# Numeric storage alone is insufficient: categorical strings cannot have a mean,
# and summing ordinal ratings is not an offered operation.
_CATEGORICAL = {
    "Attrition", "BusinessTravel", "Department", "EducationField", "Gender", "JobRole",
    "OverTime", "AgeBand", "MaritalStatus", "Over18",
}
_ADDITIVE = {
    "MonthlyIncome", "DailyRate", "HourlyRate", "MonthlyRate", "DistanceFromHome",
    "EmployeeCount", "NumCompaniesWorked", "StandardHours", "TotalWorkingYears",
    "TrainingTimesLastYear", "YearsAtCompany", "YearsInCurrentRole",
    "YearsSinceLastPromotion", "YearsWithCurrManager",
}
FIELD_METRICS = {
    name: (["count"] if name in _CATEGORICAL else
           ["count", "avg", "median"] + (["sum"] if name in _ADDITIVE else []))
    for name in FIELD_CATEGORIES
}
TRACKED_TARGETS = [name for name, metrics in FIELD_METRICS.items()
                   if any(metric in metrics for metric in ("avg", "sum", "median"))]


def _grants(*, aggregate=(), individual=()):
    return {category: (["individual"] if category in individual else [])
            + (["aggre_stats"] if category in aggregate else [])
            for category in CATEGORIES}


_NON_SENSITIVE = tuple(c for c in CATEGORIES if c != "sensitive_basic")
ROLES = {
    "ceo": {"scope": "company", "grants": _grants(aggregate=_NON_SENSITIVE)},
    "vp": {"scope": "assigned_department", "grants": _grants(aggregate=_NON_SENSITIVE)},
    "hr_head": {"scope": "company",
                "grants": _grants(aggregate=CATEGORIES, individual=CATEGORIES)},
    "hr_salary_manager": {
        "scope": "company",
        "grants": _grants(aggregate=CATEGORIES,
                           individual=("other_basic", "sensitive_basic", "salary")),
    },
    "department_hr_manager": {
        "scope": "assigned_department",
        "grants": _grants(aggregate=CATEGORIES),
    },
    "department_manager": {
        "scope": "assigned_department",
        "grants": _grants(aggregate=_NON_SENSITIVE, individual=("performance_rating",)),
    },
    "ordinary_hr": {
        "scope": "own_hr_department",
        "grants": _grants(aggregate=("performance_rating",),
                           individual=("other_basic", "sensitive_basic")),
    },
    "employee": {"scope": "none", "grants": _grants()},
}

POLICY = {
    "schema_version": 2,
    "departments": DEPARTMENTS,
    "hr_departments": ["Human Resources"],
    "field_categories": FIELD_CATEGORIES,
    "field_metrics": FIELD_METRICS,
    "field_labels": FIELD_LABELS,
    "label_reasons": LABEL_REASONS,
    "k_threshold": 5,
    "median_threshold": 10,
    "dominance_share": 0.6,
    "sensitive_targets": SENSITIVE,
    "tracked_targets": TRACKED_TARGETS,
    "quasi_identifiers": QUASI,
    "never_return": NEVER_RETURN,
    "roles": ROLES,
    "memory": {
        "scope": "team",
        "retention_days": 365,
        "retention_by_target": {"MonthlyIncome": 730, "PerformanceRating": 730},
    },
    # L5.probing: after a user's first refusal on a field, at most this many further
    # questions on it; the next one goes to the HR head for review (agent.tools._probing).
    "probing": {"max_attempts": 3, "window_days": 7},
}
