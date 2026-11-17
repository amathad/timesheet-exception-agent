"""Estimator tests: deterministic rules, database proposals, and the driver hook. No API calls."""

from datetime import date, timedelta

import pytest

from tests.test_outreach_agent import Escalator
from timesheet_exception_agent.agents.estimator import (
    ACTOR,
    CONFIDENCE_HISTORY,
    CONFIDENCE_HISTORY_CONFIRMED,
    CONFIDENCE_HISTORY_THIRD_DIFFERS,
    CONFIDENCE_STANDARD,
    COPY_RULE,
    STANDARD_RULE,
    TEAMMATE_RULE,
    EstimateError,
    Estimator,
    Facts,
    Period,
    decide,
    history_confidence,
    pay_code_check,
    standard_hours,
    weekdays,
)
from timesheet_exception_agent.agents.outreach import OutreachDriver
from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.clock import LOCK, START, SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator, load_personas
from timesheet_exception_agent.sim.seed import seed
from timesheet_exception_agent.tools.outreach import OutreachTools
from timesheet_exception_agent.tools.retrieval import HISTORY_RULE

# Exception ids follow the seed's EXCEPTIONS order.
E01, E02, E03, E08, E04, E07, E05, E06 = range(1, 9)

PERIOD_START, PERIOD_END = date(2026, 10, 12), date(2026, 10, 25)


def period(pid, hours, code="REG"):
    return Period(pid, date(2026, 9, 13) + timedelta(days=14 * (pid - 1)), hours, code)


def facts(history, job_type="full_time_hourly", hired=date(2023, 1, 9), teammates=("E-10",)):
    return Facts(
        exception_id=1,
        status="needs_fallback",
        employee_id="E-99",
        job_type=job_type,
        pay_code="REG",
        hire_date=hired,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        history=history,
        teammates=list(teammates),
    )


# Pure rules


def test_weekdays_in_a_two_week_period():
    assert weekdays(PERIOD_START, PERIOD_END) == 10
    assert weekdays(date(2026, 10, 24), date(2026, 10, 25)) == 0  # a weekend


@pytest.mark.parametrize(
    "job_type, hired, expected",
    [
        ("full_time_hourly", date(2023, 1, 9), (80.0, 10)),
        ("part_time_hourly", date(2024, 9, 30), (40.0, 10)),
        ("full_time_hourly", PERIOD_START, (80.0, 10)),  # hired on the first day
        ("full_time_hourly", date(2026, 10, 21), (24.0, 3)),  # hired Wednesday of week 2
    ],
)
def test_standard_hours_by_job_type_prorated_from_hire(job_type, hired, expected):
    assert standard_hours(job_type, hired, PERIOD_START, PERIOD_END) == expected


def test_standard_hours_refuses_unknown_job_type_and_future_hire():
    with pytest.raises(EstimateError, match="job type"):
        standard_hours("contractor", date(2023, 1, 9), PERIOD_START, PERIOD_END)
    with pytest.raises(EstimateError, match="after the period"):
        standard_hours("full_time_hourly", date(2026, 11, 2), PERIOD_START, PERIOD_END)


def test_history_confidence_placeholders():
    assert history_confidence([period(3, 80), period(2, 80), period(1, 80)]) == (
        CONFIDENCE_HISTORY_CONFIRMED
    )
    assert history_confidence([period(3, 80), period(2, 80)]) == CONFIDENCE_HISTORY
    assert history_confidence([period(3, 80), period(2, 80), period(1, 72)]) == (
        CONFIDENCE_HISTORY_THIRD_DIFFERS
    )


def test_pay_code_check():
    assert pay_code_check([period(3, 80), period(2, 80)]) is None
    note = pay_code_check([period(3, 80, "PTO"), period(2, 80)])
    assert "REG, then PTO" in note and COPY_RULE in note


def test_decide_copies_agreeing_history():
    p = decide(facts([period(3, 80), period(2, 80), period(1, 80)]))
    assert (p.path, p.method, p.hours, p.pay_code) == ("history", "history", 80, "REG")
    assert p.confidence == CONFIDENCE_HISTORY_CONFIRMED
    assert COPY_RULE in p.reason and "Proposal only" in p.reason
    assert p.basis["history_note"] is None


def test_decide_conflicting_history_goes_to_teammate_with_standard_backup():
    p = decide(facts([period(3, 64), period(2, 80), period(1, 80)], teammates=("E-04", "E-10")))
    assert (p.path, p.method, p.hours) == ("teammate", "standard_hours", 80.0)
    assert p.confidence == CONFIDENCE_STANDARD
    assert "80, then 64" in p.reason
    for rule in (HISTORY_RULE, TEAMMATE_RULE, STANDARD_RULE):
        assert rule in p.reason
    assert "E-04, E-10" in p.reason
    assert p.basis["standard_hours"] == {"weekdays": 10, "hours_per_day": 8.0}


def test_decide_mixed_pay_codes_fail_even_when_hours_agree():
    p = decide(facts([period(3, 80, "PTO"), period(2, 80)]))
    assert p.path == "teammate" and p.method == "standard_hours"


def test_decide_without_teammates_uses_standard_hours_alone():
    p = decide(facts([], teammates=()))
    assert (p.path, p.method, p.hours) == ("standard_hours", "standard_hours", 80.0)
    assert "No teammate on file" in p.reason and TEAMMATE_RULE not in p.reason


def test_decide_when_every_teammate_has_an_open_exception():
    f = Facts(**{**facts([], teammates=()).__dict__, "teammates_excluded": ["E-04"]})
    p = decide(f)
    assert p.path == "standard_hours" and TEAMMATE_RULE not in p.reason
    assert "E-04 have open exceptions of their own" in p.reason
    assert p.basis["teammates_excluded"] == ["E-04"]


# Database (rolled back)


@pytest.fixture
def conn():
    with get_conn() as c:
        seed(c)
        yield c
        c.rollback()


@pytest.fixture
def clock():
    return SimClock()


@pytest.fixture
def tools(conn, clock):
    return OutreachTools(conn, clock, ReplySimulator(load_personas(conn)))


@pytest.fixture
def estimator(conn, clock):
    return Estimator(conn, clock)


def estimates(conn):
    rows = conn.execute(
        "SELECT exception_id, path, method, hours, pay_code, confidence, status,"
        " approved_by, approved_at FROM estimates ORDER BY exception_id"
    ).fetchall()
    return {
        r[0]: {
            "path": r[1],
            "method": r[2],
            "hours": float(r[3]),
            "pay_code": r[4],
            "confidence": float(r[5]),
            "status": r[6],
            "approved_by": r[7],
            "approved_at": r[8],
        }
        for r in rows
    }


def estimator_audit(conn):
    return conn.execute(
        "SELECT action, exception_id, details FROM audit_log WHERE actor = %s ORDER BY id",
        (ACTOR,),
    ).fetchall()


def test_seed_scenarios(conn, tools, estimator):
    tools.hand_off([E04, E07, E06], "test hand-off")
    assert estimator.sync() == [E04, E07, E06]  # exception id order
    got = estimates(conn)
    assert got[E04] == {
        "path": "history",
        "method": "history",
        "hours": 80.0,
        "pay_code": "REG",
        "confidence": CONFIDENCE_HISTORY_CONFIRMED,
        "status": "proposed",
        "approved_by": None,
        "approved_at": None,
    }
    # E-07: 80 then 64 hours; E-06: new hire with no history, hired on the period's first day.
    for exception_id in (E07, E06):
        assert got[exception_id]["path"] == "teammate"
        assert got[exception_id]["method"] == "standard_hours"
        assert got[exception_id]["hours"] == 80.0
        assert got[exception_id]["confidence"] == CONFIDENCE_STANDARD
    basis = conn.execute(
        "SELECT basis FROM estimates WHERE exception_id = %s", (E06,)
    ).fetchone()[0]
    assert basis["teammates"] == ["E-09"] and basis["history_newest_first"] == []


def test_teammates_with_open_exceptions_are_excluded(conn, tools, estimator):
    # M-03 manages E-04, E-07 and E-10. E-04 has its own open exception, so for E-07 only
    # E-10 can be asked; E-04 is kept in the basis as excluded.
    f = estimator.load_facts(E07)
    assert f.teammates == ["E-10"] and f.teammates_excluded == ["E-04"]
    tools.hand_off([E07], "test hand-off")
    estimator.sync()
    reason, basis = conn.execute(
        "SELECT reason, basis FROM estimates WHERE exception_id = %s", (E07,)
    ).fetchone()
    assert "Ask a teammate (E-10) first" in reason
    assert basis["teammates"] == ["E-10"] and basis["teammates_excluded"] == ["E-04"]


def test_teammate_already_asked_is_not_asked_again(conn, tools, estimator):
    # Run Q finding: Outreach emailed E-09 about E-06, and the proposal still said to ask.
    tools.send_message(E06, "E-09", "email", "Can you confirm Finley's hours?")
    tools.hand_off([E06], "test hand-off")
    estimator.sync()
    reason, basis = conn.execute(
        "SELECT reason, basis FROM estimates WHERE exception_id = %s", (E06,)
    ).fetchone()
    assert "E-09 was already asked" in reason and "Ask a teammate" not in reason
    assert basis["teammates_contacted"] == ["E-09"]


def test_duplicate_exception_does_not_exclude_a_teammate(conn, estimator):
    conn.execute("UPDATE exceptions SET status = 'duplicate' WHERE id = %s", (E04,))
    assert estimator.load_facts(E07).teammates == ["E-04", "E-10"]


def test_teammate_becomes_eligible_once_their_exception_is_resolved(conn, estimator):
    conn.execute("UPDATE exceptions SET status = 'resolved' WHERE id = %s", (E04,))
    assert estimator.load_facts(E07).teammates == ["E-04", "E-10"]


def test_every_proposal_is_audited(conn, tools, estimator, clock):
    tools.hand_off([E04], "test hand-off")
    estimator.sync()
    rows = conn.execute(
        "SELECT action, reason, details, sim_time FROM audit_log"
        " WHERE actor = %s AND exception_id = %s",
        (ACTOR, E04),
    ).fetchall()
    assert len(rows) == 1
    action, reason, details, when = rows[0]
    assert action == "propose_estimate" and COPY_RULE in reason
    assert details["hours"] == 80.0 and details["path"] == "history"
    assert details["replaced"] is False and when == clock.now()


def test_only_handed_off_exceptions_get_proposals(conn, estimator):
    assert estimator.sync() == []
    with pytest.raises(EstimateError, match="not needs_fallback"):
        estimator.propose(E04)
    assert estimates(conn) == {}


def test_sync_is_idempotent_and_propose_replaces(conn, tools, estimator):
    tools.hand_off([E04], "test hand-off")
    estimator.sync()
    assert estimator.sync() == []
    estimator.propose(E04)
    assert conn.execute("SELECT count(*) FROM estimates").fetchone()[0] == 1
    actions = [(a, d["replaced"]) for a, _, d in estimator_audit(conn)]
    assert actions == [("propose_estimate", False), ("propose_estimate", True)]


def test_estimator_never_approves_or_changes_time(conn, tools, estimator):
    history_before = conn.execute("SELECT count(*), sum(hours) FROM timesheet_history").fetchone()
    tools.hand_off([E04, E07, E06], "test hand-off")
    estimator.sync()
    statuses = dict(conn.execute("SELECT id, status FROM exceptions").fetchall())
    assert all(statuses[x] == "needs_fallback" for x in (E04, E07, E06))
    assert conn.execute("SELECT count(*), sum(hours) FROM timesheet_history").fetchone() == (
        history_before
    )
    assert all(
        e["status"] == "proposed" and e["approved_by"] is None and e["approved_at"] is None
        for e in estimates(conn).values()
    )
    assert not hasattr(estimator, "approve")


def test_late_manager_decision_withdraws_the_proposal(conn, tools, estimator):
    tools.hand_off([E04], "test hand-off")
    estimator.sync()
    tools.update_status(E04, "resolved", "next-level manager approved late")
    estimator.sync()
    assert estimates(conn)[E04]["status"] == "withdrawn"
    assert [a for a, *_ in estimator_audit(conn)] == ["propose_estimate", "withdraw_estimate"]


def test_coordinator_decisions_are_not_overwritten(conn, tools, estimator):
    tools.hand_off([E04], "test hand-off")
    estimator.sync()
    conn.execute("UPDATE estimates SET status = 'approved', approved_by = 'AM'")
    with pytest.raises(EstimateError, match="marked approved"):
        estimator.propose(E04)


def test_unestimable_exception_is_skipped_once_and_audited(conn, tools, estimator):
    conn.execute("UPDATE employees SET job_type = 'contractor' WHERE id = 'E-06'")
    tools.hand_off([E06], "test hand-off")
    assert estimator.sync() == []
    assert estimator.sync() == []
    rows = estimator_audit(conn)
    assert [(a, x) for a, x, _ in rows] == [("estimate_skipped", E06)]
    assert estimates(conn) == {}


# Driver hook (the driver commits, so start from a committed seed)


@pytest.fixture
def committed_conn():
    with get_conn() as c:
        seed(c)
        c.commit()
        yield c
        c.rollback()


def run_morning(conn, clock, overrides=None):
    sim = ReplySimulator(load_personas(conn), overrides=overrides)
    tools = OutreachTools(conn, clock, sim)
    driver = OutreachDriver(conn, clock, tools, Escalator(tools), estimator=Estimator(conn, clock))
    driver.run()


def proposed_at(conn, exception_id):
    return conn.execute(
        "SELECT sim_time FROM audit_log WHERE action = 'propose_estimate'"
        " AND exception_id = %s",
        (exception_id,),
    ).fetchone()[0]


def test_driver_proposes_for_every_hand_off(committed_conn, clock):
    run_morning(committed_conn, clock, overrides={"N-02": "silent"})
    got = estimates(committed_conn)
    assert set(got) == {E04, E07, E06}
    assert got[E04]["path"] == "history" and got[E07]["path"] == "teammate"
    # E-04/E-07 are handed off by the 30-minute timeout at 09:00; E-06 only at the lock.
    assert proposed_at(committed_conn, E04) == START + timedelta(minutes=60)
    assert proposed_at(committed_conn, E06) >= LOCK


def test_driver_withdraws_after_late_next_level_approval(committed_conn, clock):
    run_morning(committed_conn, clock)  # N-02 is slow: approves after the hand-off
    got = estimates(committed_conn)
    assert got[E04]["status"] == got[E07]["status"] == "withdrawn"
    assert got[E06]["status"] == "proposed"


def test_driver_without_estimator_writes_no_estimates(committed_conn, clock):
    sim = ReplySimulator(load_personas(committed_conn))
    tools = OutreachTools(committed_conn, clock, sim)
    OutreachDriver(committed_conn, clock, tools, Escalator(tools)).run()
    assert estimates(committed_conn) == {}
