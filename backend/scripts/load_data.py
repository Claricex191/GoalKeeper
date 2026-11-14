"""Load IBM HR Analytics into Atlas.

- read backend/data/ibm_hr.csv if present, else
  sklearn.datasets.fetch_openml(data_id=43896, as_frame=True) and save that CSV
- check: 1,470 rows, 35 columns, EmployeeNumber present and unique
- convert numpy / categorical values to plain Python int / str before inserting
- add per employee:
  - `active` = True
  - `AgeBand` (5-year bands, e.g. "25-29")
  - `start_date` = a date well before the demo (e.g. 2000-01-01)
  - `updated_at` = {}   (no sensitive field modified since load)
- new-hire demo: EmployeeNumber 473 (Sales / Manager, YearsAtCompany == 0) gets
  start_date = the demo "Thursday" (a constant here), so the warmup "Monday" queries
  run without them and advancing the clock adds them. Every other employee, including
  1306 (R&D / Manager, used in the three-query demo), is present from the start.
- insert into `employees`; create the indexes listed in docs/collections.md
"""

if __name__ == "__main__":
    raise NotImplementedError
