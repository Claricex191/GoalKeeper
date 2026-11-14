"""Regressions for releases whose value versions cannot be recovered from timestamps."""

from datetime import timedelta

from app.gate import check, cross_query, evidence
from test_gate import INCOME, MON, POLICY, THU, WED, cell, query, release


def test_update_at_release_time_blocks_the_repeat():
    before = cell(range(6), vmin=900, vmax=1100)
    before.value = 1000
    after = cell(range(6), vmin=900, vmax=1700)
    after.value = 1100
    assert check(query(), "manager", [before], [], {}, POLICY).status == "allowed"
    history = [release(before.ids, at=MON)]
    decision = check(query(), "manager", [after], history, {0: {INCOME: MON}}, POLICY)
    # Returning after would reveal the one employee's 600-unit raise.
    assert decision.status == "blocked"
    assert decision.rule == "L4.history"


def test_tied_update_does_not_push_four_changes_above_the_threshold():
    history = [release(range(6), at=MON)]
    decision = check(query(), "manager", [cell(range(10))], history,
                     {0: {INCOME: MON}}, POLICY)
    # The update could precede the old release, leaving only four changed people.
    assert decision.status == "blocked"
    assert decision.rule == "L4.history"


def test_second_update_cannot_erase_a_historical_raise():
    managers, others = set(range(6)), set(range(6, 12))
    first = cell(managers)
    assert check(query(), "manager", [first], [], {}, POLICY).status == "allowed"
    history = [release(managers, at=MON)]
    # The wider release is safe with one old value version, after the first raise.
    wider = cell(managers | others)
    assert check(query(), "manager", [wider], history,
                 {0: {INCOME: WED}}, POLICY).status == "allowed"
    history.append(release(wider.ids, at=THU))
    latest_update = THU + timedelta(days=1)
    decision = check(query(), "manager", [cell(others)], history,
                     {0: {INCOME: latest_update}}, POLICY)
    # Wider - managers - others would reveal the first raise, even though
    # employee 0 appears only in the old releases, not in the new result.
    assert decision.status == "blocked"
    assert decision.rule == "L4.history"


def test_existing_history_with_tied_release_dates_is_also_ambiguous():
    # A legacy history can contain two observations around an earlier same-tick
    # update. A later update replaces its timestamp, hiding that earlier change.
    history = [release(range(6), at=MON), release(range(12), at=MON)]
    decision = check(query(), "manager", [cell(range(6, 12))], history,
                     {0: {INCOME: WED}}, POLICY)
    assert decision.status == "blocked"
    assert decision.rule == "L4.history"


def test_ambiguity_connected_through_multiple_old_releases_is_checked():
    managers, others, third = set(range(6)), set(range(6, 12)), set(range(12, 18))
    history = [release(managers, at=MON), release(managers | others, at=THU),
               release(others | third, at=THU)]
    decision = check(query(), "manager", [cell(third)], history,
                     {0: {INCOME: THU + timedelta(days=1)}}, POLICY)
    assert decision.status == "blocked"
    assert decision.rule == "L4.history"


def test_disconnected_history_does_not_block_a_safe_query():
    history = [release(range(6), at=MON), release(range(12), at=THU)]
    decision = check(query(), "manager", [cell(range(100, 106))], history,
                     {0: {INCOME: THU + timedelta(days=1)}}, POLICY)
    assert decision.status == "allowed"


def test_unrelated_targets_and_updates_do_not_create_ambiguity():
    history = [release(range(6), at=MON, target="JobSatisfaction")]
    decision = check(query(), "manager", [cell(range(6))], history,
                     {0: {INCOME: MON}}, POLICY)
    assert decision.status == "allowed"
    history = [release(range(6), at=MON)]
    decision = check(query(), "manager", [cell(range(6))], history,
                     {0: {"JobSatisfaction": MON}}, POLICY)
    assert decision.status == "allowed"


def test_strictly_post_update_repeat_and_empty_results_stay_allowed():
    history = [release(range(6), at=THU)]
    updated = {0: {INCOME: WED}}
    assert check(query(), "manager", [cell(range(6))], history,
                 updated, POLICY).status == "allowed"
    assert check(query(), "manager", [], history,
                 {0: {INCOME: THU}}, POLICY).status == "allowed"


def test_ambiguous_history_is_generic_in_public_and_explained_in_audit():
    decision = check(query(), "manager", [cell(range(6))],
                     [release(range(6), label="private question")],
                     {0: {INCOME: MON}}, POLICY)
    public = evidence.public_view(decision, viewer="mgr_2")
    assert decision.rule == "L4.history"
    assert public["rule"] == "L4"
    assert public["cells"] == []
    assert "timestamp" not in public["evidence_text"]
    assert "private question" not in public["evidence_text"]
    assert "timestamp" in evidence.render_audit(decision)


def test_linear_helper_rejects_ambiguous_historical_versions():
    history = [release(range(6), at=MON), release(range(12), at=THU)]
    found = cross_query.linear([cell(range(6, 12))], history, INCOME,
                               {0: {INCOME: THU + timedelta(days=1)}})
    assert found is not None
    assert found["rule"] == "L4.history"
