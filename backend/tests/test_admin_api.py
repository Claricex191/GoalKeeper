"""Exercise the real FastAPI routes with an isolated database dependency."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app import clock
from app.policy.defaults import POLICY
from app.schemas import ChatResponse
from app.users import DEMO_USERS


@pytest.fixture
def api(monkeypatch):
    # Loading just the route module with a stub DB avoids reading credentials,
    # changing app.config's singleton, or altering the optional Atlas tests.
    db_module = ModuleType("app.db")
    db_module.get_db = Mock()
    db_module.ensure_indexes = Mock()
    loop_module = ModuleType("app.agent.loop")
    loop_module.run_agent = Mock()
    loop_module.SESSIONS = {}
    spec = importlib.util.spec_from_file_location(
        "admin_routes_under_test", Path(__file__).parents[1] / "app" / "main.py")
    main = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "app.db", db_module)
        patch.setitem(sys.modules, "app.agent.loop", loop_module)
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
    policy = {**deepcopy(POLICY), "dataset": "ibm_hr", "version": 2, "status": "active"}
    monkeypatch.setattr(main.store, "load_active", Mock(return_value=policy))
    monkeypatch.setattr(main.executor, "field_types", Mock(return_value={
        "MonthlyIncome": int, "Attrition": str, "JobSatisfaction": int,
        "PerformanceRating": int, "YearsAtCompany": int, "Age": int}))
    at = clock.DEMO_START + timedelta(days=2)
    monkeypatch.setattr(main.clock, "now", Mock(return_value=at))
    return SimpleNamespace(main=main, client=TestClient(main.app), db=db, session=session, at=at)


HEADERS = {"X-Demo-User": "hr_1"}
ADMIN_MUTATIONS = [
    ("/admin/clock", {"days": 1}),
    ("/admin/reset", {}),
    ("/admin/simulate-update", {"employee_number": 1408, "field": "MonthlyIncome",
                                "new_value": "18500"}),
    ("/admin/policy/draft", {"dataset": "ibm_hr"}),
    ("/admin/policy/1/activate", {"dataset": "ibm_hr"}),
]


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


@pytest.mark.parametrize("path,params", ADMIN_MUTATIONS)
@pytest.mark.parametrize("user", [who.user for who in DEMO_USERS.values()
                                  if who.role != "hr_head"])
def test_only_hr_head_can_mutate_demo_state(api, path, params, user):
    response = api.client.post(path, params=params, headers={"X-Demo-User": user})
    assert response.status_code == 403
    api.main.get_db.assert_not_called()


@pytest.mark.parametrize("user", list(DEMO_USERS))
def test_all_authenticated_users_can_read_clock(api, user):
    assert api.client.get("/admin/clock", headers={"X-Demo-User": user}).status_code == 200


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
                            "field": "MonthlyIncome", "user": "hr_1", "time": api.at}
    assert event_kwargs["session"] is api.session
    api.main.clock.now.assert_called_once_with(db=api.db, session=api.session)
    assert api.db.meta.update_one.call_args.kwargs["session"] is api.session
    api.main.store.load_active.assert_called_once_with(api.db, "ibm_hr", session=api.session)
    api.main.executor.field_types.assert_called_once_with(api.db, api.at, scope=None,
                                                          session=api.session)


@pytest.mark.parametrize("value", ["abc", "18500.5", "NaN", "Inf", "9223372036854775808"])
def test_invalid_integer_update_never_writes(api, value):
    assert update(api, new_value=value).status_code == 400
    api.db.employees.update_one.assert_not_called()
    api.db.ledger.insert_one.assert_not_called()


def test_integer_update_does_not_round_through_float(api):
    assert update(api, new_value="9007199254740993").status_code == 200
    assert api.db.employees.update_one.call_args.args[1]["$set"]["MonthlyIncome"] == 9007199254740993


def test_age_crossing_band_updates_derived_field_in_same_transaction(api):
    api.db.employees.find_one.return_value = {"Age": 29, "AgeBand": "25-29"}
    assert update(api, field="Age", new_value="30").json() == {"ok": True}
    api.db.employees.find_one.assert_called_once_with(
        {"EmployeeNumber": 1408}, {"AgeBand": 1, "_id": 0}, session=api.session)
    args, kwargs = api.db.employees.update_one.call_args
    assert args[1] == {"$set": {"Age": 30, "updated_at.Age": api.at,
                                "AgeBand": "30-34", "updated_at.AgeBand": api.at}}
    assert kwargs["session"] is api.session
    api.db.ledger.insert_one.assert_called_once_with(
        {"kind": "data_update", "employee_number": 1408, "field": "Age",
         "user": "hr_1", "time": api.at}, session=api.session)
    api.main.clock.now.assert_called_once_with(db=api.db, session=api.session)


def test_age_within_same_band_preserves_derived_timestamp(api):
    api.db.employees.find_one.return_value = {"Age": 27, "AgeBand": "25-29"}
    assert update(api, field="Age", new_value="28").status_code == 200
    changes = api.db.employees.update_one.call_args.args[1]["$set"]
    assert changes == {"Age": 28, "updated_at.Age": api.at, "AgeBand": "25-29"}
    assert "updated_at.AgeBand" not in changes


def test_age_update_missing_employee_never_writes_or_audits(api):
    api.db.employees.find_one.return_value = None
    assert update(api, field="Age", new_value="30").status_code == 404
    api.db.employees.update_one.assert_not_called()
    api.db.ledger.insert_one.assert_not_called()


@pytest.mark.parametrize("field,value,status", [
    ("Attrition", "Yes", 400), ("Attrition", "Maybe", 400),
    ("JobSatisfaction", "NaN", 400), ("JobSatisfaction", "4.5", 400),
    ("JobSatisfaction", "4", 200), ("PerformanceRating", "3", 200),
    ("YearsAtCompany", "6", 200),
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


def test_legacy_active_policy_is_503(api, monkeypatch):
    monkeypatch.setattr(api.main.store, "load_active",
                        Mock(side_effect=ValueError("policy schema_version must be 2")))
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
    api.main.SESSIONS[("mgr_1", "s", True)] = [{"role": "user", "content": "old question"}]
    assert api.client.post("/admin/reset", headers=HEADERS).json() == {
        "ok": True, "now": clock.DEMO_START.isoformat()}
    reset.assert_called_once_with(api.db)
    assert api.main.SESSIONS == {}
    history = [{"role": "user", "content": "keep until reset commits"}]
    api.main.SESSIONS[("mgr_1", "s", True)] = history
    reset.side_effect = LookupError("missing baseline")
    assert api.client.post("/admin/reset", headers=HEADERS).status_code == 503
    assert api.main.SESSIONS[("mgr_1", "s", True)] == history
    reset.side_effect = ValueError("invalid state")
    assert api.client.post("/admin/reset", headers=HEADERS).status_code == 409
    assert api.main.SESSIONS[("mgr_1", "s", True)] == history


def test_policy_activation_uses_hr_head_identity_not_supplied_approver(api, monkeypatch):
    activate = Mock()
    monkeypatch.setattr(api.main.store, "activate", activate)
    params = {"dataset": "ibm_hr", "approved_by": "spoofed"}
    assert api.client.post("/admin/policy/2/activate", params=params,
                           headers=HEADERS).json() == {"ok": True}
    activate.assert_called_once_with(api.db, "ibm_hr", 2, approved_by="hr_1")


@pytest.mark.parametrize("error,status", [(LookupError("missing version"), 404),
                                          (ValueError("legacy policy"), 400)])
def test_policy_activation_errors_are_client_errors(api, monkeypatch, error, status):
    monkeypatch.setattr(api.main.store, "activate", Mock(side_effect=error))
    assert api.client.post("/admin/policy/1/activate", params={"dataset": "ibm_hr"},
                           headers=HEADERS).status_code == status


def test_chat_forwards_to_imported_agent_with_server_identity(api, monkeypatch):
    agent = Mock(return_value=ChatResponse(reply="Test answer", trace=[]))
    monkeypatch.setattr(api.main, "run_agent", agent)
    body = {"project": "comp-review-2026", "session_id": "s1",
            "message": "What is the average salary?", "memory": True}
    response = api.client.post("/chat", json=body, headers={"X-Demo-User": "mgr_1"})
    assert response.json() == {"reply": "Test answer", "trace": []}
    request, who = agent.call_args.args
    assert request.model_dump() == body
    assert who is DEMO_USERS["mgr_1"]


def test_pending_reviews_forward_to_imported_approvals(api, monkeypatch):
    pending = Mock(return_value=[{"id": "approval-1"}])
    monkeypatch.setattr(api.main.approvals, "list_pending", pending)
    assert api.client.get("/review/pending", headers=HEADERS).json() == [{"id": "approval-1"}]
    pending.assert_called_once_with(api.db)


def test_review_decision_forwards_to_imported_approvals(api, monkeypatch):
    decide = Mock(return_value=True)
    monkeypatch.setattr(api.main.approvals, "decide", decide)
    response = api.client.post("/review/approval-1",
                               params={"approve": "true", "transform": "T3_widen_scope"},
                               headers=HEADERS)
    assert response.json() == {"ok": True}
    decide.assert_called_once_with(api.db, "approval-1", True, "T3_widen_scope", "hr_1", api.at)
    decide.return_value = False
    assert api.client.post("/review/approval-1", params={"approve": "false"},
                           headers=HEADERS).status_code == 404


def test_review_routes_reject_non_hr_head_before_db(api):
    headers = {"X-Demo-User": "salary_1"}
    assert api.client.get("/review/pending", headers=headers).status_code == 403
    assert api.client.post("/review/approval-1", params={"approve": "true"},
                           headers=headers).status_code == 403
    api.main.get_db.assert_not_called()
