"""Exercise the real FastAPI routes with an isolated database dependency."""

from contextlib import contextmanager
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app import clock


@pytest.fixture
def api(monkeypatch):
    # Loading just the route module with a stub DB avoids reading credentials,
    # changing app.config's singleton, or altering the optional Atlas tests.
    db_module = ModuleType("app.db")
    db_module.get_db = Mock()
    db_module.ensure_indexes = Mock()
    spec = importlib.util.spec_from_file_location(
        "admin_routes_under_test", Path(__file__).parents[1] / "app" / "main.py")
    main = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "app.db", db_module)
        spec.loader.exec_module(main)

    session = Mock()
    session.with_transaction.side_effect = lambda callback: callback(session)

    @contextmanager
    def start_session():
        yield session

    db = SimpleNamespace(employees=Mock(), ledger=Mock(), meta=Mock(),
                         client=SimpleNamespace(start_session=start_session))
    db.employees.update_one.return_value = SimpleNamespace(matched_count=1, modified_count=0)
    db_module.get_db.return_value = db
    monkeypatch.setattr(main.store, "load_active", lambda *_, **__: {
        "sensitive_targets": ["MonthlyIncome", "Attrition", "JobSatisfaction"]})
    monkeypatch.setattr(main.executor, "field_types", lambda _, **__: {
        "MonthlyIncome": int, "Attrition": str, "JobSatisfaction": float})
    at = clock.DEMO_START + timedelta(days=2)
    monkeypatch.setattr(main.clock, "now", Mock(return_value=at))
    return SimpleNamespace(main=main, client=TestClient(main.app), db=db, session=session, at=at)


HEADERS = {"X-Demo-User": "mgr_1"}


def update(api, **changes):
    params = {"employee_number": 1408, "field": "MonthlyIncome", "new_value": "18500"}
    return api.client.post("/admin/simulate-update", params={**params, **changes}, headers=HEADERS)


@pytest.mark.parametrize("method,path,params", [
    ("get", "/admin/clock", {}), ("post", "/admin/clock", {"days": 1}),
    ("post", "/admin/reset", {}),
    ("post", "/admin/simulate-update", {"employee_number": 1408, "field": "MonthlyIncome",
                                         "new_value": "18500"}),
    ("post", "/admin/policy/draft", {"dataset": "ibm_hr"}),
    ("post", "/admin/policy/1/activate", {"dataset": "ibm_hr"}),
])
@pytest.mark.parametrize("headers", [{}, {"X-Demo-User": "unknown"}])
def test_admin_routes_require_identity(api, method, path, params, headers):
    assert api.client.request(method, path, params=params, headers=headers).status_code == 401


def test_clock_contract(api, monkeypatch):
    got = api.client.get("/admin/clock", headers=HEADERS)
    assert got.json() == {"now": api.at.isoformat(), "offset_days": 2}
    monkeypatch.setattr(api.main.clock, "advance", lambda *a, **k: api.at)
    assert api.client.post("/admin/clock", params={"days": 2}, headers=HEADERS).json() == got.json()


@pytest.mark.parametrize("value", ["NaN", "Inf", "-Inf", "0", "-1", "1e100", "1e-15"])
def test_clock_bad_days_are_client_errors(api, value):
    response = api.client.post("/admin/clock", params={"days": value}, headers=HEADERS)
    assert response.status_code == 400
    api.db.meta.update_one.assert_not_called()


def test_update_records_integer_and_one_transaction_timestamp(api):
    assert update(api).json() == {"ok": True}
    args, kwargs = api.db.employees.update_one.call_args
    assert args[0] == {"EmployeeNumber": 1408}
    assert type(args[1]["$set"]["MonthlyIncome"]) is int
    assert args[1]["$set"]["MonthlyIncome"] == 18500
    assert args[1]["$set"]["updated_at.MonthlyIncome"] == api.at
    assert kwargs["session"] is api.session
    event_args, event_kwargs = api.db.ledger.insert_one.call_args
    assert event_args[0] == {"kind": "data_update", "employee_number": 1408,
                            "field": "MonthlyIncome", "user": "mgr_1", "time": api.at}
    assert event_kwargs["session"] is api.session
    api.main.clock.now.assert_called_once_with(db=api.db, session=api.session)
    assert api.db.meta.update_one.call_args.kwargs["session"] is api.session


@pytest.mark.parametrize("value", ["abc", "18500.5", "NaN", "Inf", "9223372036854775808"])
def test_invalid_integer_update_never_writes(api, value):
    assert update(api, new_value=value).status_code == 400
    api.db.employees.update_one.assert_not_called()
    api.db.ledger.insert_one.assert_not_called()


def test_integer_update_does_not_round_through_float(api):
    assert update(api, new_value="9007199254740993").status_code == 200
    assert api.db.employees.update_one.call_args.args[1]["$set"]["MonthlyIncome"] == 9007199254740993


@pytest.mark.parametrize("field,value,status", [
    ("Attrition", "Yes", 200), ("Attrition", "Maybe", 400),
    ("JobSatisfaction", "NaN", 400), ("JobSatisfaction", "4.5", 200),
    ("EmployeeNumber", "1", 400), ("updated_at.MonthlyIncome", "1", 400),
])
def test_update_field_validation(api, field, value, status):
    assert update(api, field=field, new_value=value).status_code == status


def test_missing_employee_is_404_and_not_audited(api):
    api.db.employees.update_one.return_value = SimpleNamespace(matched_count=0)
    assert update(api).status_code == 404
    api.db.ledger.insert_one.assert_not_called()


def test_monday_update_requires_clock_advance(api):
    api.main.clock.now.return_value = clock.DEMO_START
    assert update(api).status_code == 409
    api.db.employees.update_one.assert_not_called()


def test_failed_audit_does_not_report_success(api):
    api.db.ledger.insert_one.side_effect = RuntimeError("audit unavailable")
    with pytest.raises(RuntimeError, match="audit unavailable"):
        update(api)


def test_missing_policy_is_503(api, monkeypatch):
    monkeypatch.setattr(api.main.store, "load_active", Mock(side_effect=LookupError("load policy")))
    assert update(api).status_code == 503
    api.db.employees.update_one.assert_not_called()


def test_missing_employee_data_is_503(api, monkeypatch):
    monkeypatch.setattr(api.main.executor, "field_types",
                        Mock(side_effect=api.main.executor.QueryError("employees is empty")))
    assert update(api).status_code == 503
    api.db.employees.update_one.assert_not_called()


def test_employee_number_outside_bson_range_is_rejected(api):
    assert update(api, employee_number=2 ** 63).status_code == 422
    api.db.employees.update_one.assert_not_called()


def test_reset_response_and_failure_status(api, monkeypatch):
    reset = Mock(return_value=clock.DEMO_START)
    monkeypatch.setattr(api.main, "reset_demo", reset)
    assert api.client.post("/admin/reset", headers=HEADERS).json() == {
        "ok": True, "now": clock.DEMO_START.isoformat()}
    reset.assert_called_once_with(api.db)
    reset.side_effect = LookupError("missing baseline")
    assert api.client.post("/admin/reset", headers=HEADERS).status_code == 503
    reset.side_effect = ValueError("invalid state")
    assert api.client.post("/admin/reset", headers=HEADERS).status_code == 409


def test_policy_activation_uses_hr_identity_not_supplied_approver(api, monkeypatch):
    activate = Mock()
    monkeypatch.setattr(api.main.store, "activate", activate)
    params = {"dataset": "ibm_hr", "approved_by": "spoofed"}
    assert api.client.post("/admin/policy/2/activate", params=params, headers=HEADERS).status_code == 403
    assert api.client.post("/admin/policy/draft", params=params, headers=HEADERS).status_code == 403
    assert api.client.post("/admin/policy/2/activate", params=params,
                           headers={"X-Demo-User": "hr_1"}).json() == {"ok": True}
    activate.assert_called_once_with(api.db, "ibm_hr", 2, approved_by="hr_1")
