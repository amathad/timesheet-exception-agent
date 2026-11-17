from datetime import timedelta

import pytest

from timesheet_exception_agent.agents.estimator import Estimator
from timesheet_exception_agent.agents.intake import Intake
from timesheet_exception_agent.agents.outreach import REASONS, Wake
from timesheet_exception_agent.agents.router import ACTOR, Router
from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.clock import SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator, load_personas
from timesheet_exception_agent.sim.seed import seed
from timesheet_exception_agent.tools.outreach import OutreachTools

E01, E02, E03, E08, E04, E07, E05, E06 = range(1, 9)


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
def router(conn, clock):
    intake = Intake(conn, clock)
    intake.run()
    return Router(conn, clock, intake)


def test_manager_out_reason_is_known_to_the_agent():
    assert "manager_out" in REASONS


def test_presence_marks_managers_out(router):
    assert router.managers_out() == {"M-02", "M-05"}


def test_plan_orders_wakes_by_risk(router):
    wakes = [Wake(x, "first_contact") for x in (E01, E03, E06, E07)]
    assert [w.exception_id for w in router.plan(wakes)] == [E06, E03, E07, E01]


def test_contacted_exceptions_of_an_out_manager_wake_once(conn, router, tools):
    # The agent messaged M-02 on Teams for both (a mistake the router corrects).
    tools.send_message(E03, "M-02", "teams", "Please approve.")
    tools.send_message(E08, "M-02", "teams", "Please submit.")
    first = router.plan([])
    assert {(w.exception_id, w.reason) for w in first} == {
        (E03, "manager_out"),
        (E08, "manager_out"),
    }
    assert router.plan([]) == []  # once per exception
    audited = conn.execute(
        "SELECT exception_id FROM audit_log WHERE actor = %s ORDER BY exception_id", (ACTOR,)
    ).fetchall()
    assert [r[0] for r in audited] == [E03, E08]


def test_auto_reply_spreads_to_the_managers_other_exceptions(conn, router, tools, clock):
    conn.execute("UPDATE presence SET status = 'available' WHERE person_id = 'M-01'")
    tools.send_message(E01, "M-01", "teams", "Please submit.")
    tools.send_message(E02, "M-01", "teams", "Please approve.")
    assert router.plan([]) == []
    conn.execute(
        "INSERT INTO messages (exception_id, person_id, channel, direction, body, sim_time)"
        " VALUES (%s, 'M-01', 'email', 'received', 'Automatic reply: I am out.', %s)",
        (E01, clock.now()),
    )
    planned = router.plan([Wake(E01, "new_reply")])
    assert {(w.exception_id, w.reason) for w in planned} == {
        (E01, "new_reply"),  # its own reply wake is kept
        (E02, "manager_out"),
    }


def test_manager_out_replaces_a_follow_up_but_not_the_cutoff(conn, router, tools):
    tools.send_message(E03, "M-02", "teams", "Please approve.")
    tools.send_message(E08, "M-02", "teams", "Please submit.")
    planned = router.plan([Wake(E03, "follow_up_due"), Wake(E08, "escalation_due")])
    assert {(w.exception_id, w.reason) for w in planned} == {
        (E03, "manager_out"),
        (E08, "escalation_due"),
    }


def test_escalated_exceptions_are_left_alone(router, tools):
    tools.send_message(E03, "N-01", "email", "M-02 is out; please approve.")
    assert router.plan([]) == []


def test_approval_queue_lowest_confidence_first(conn, router, tools, clock):
    tools.hand_off([E04, E07, E06], "test")
    Estimator(conn, clock).sync()
    queue = router.approval_queue()
    assert [q["exception_id"] for q in queue] == [E07, E06, E04]
    assert queue[0]["confidence"] == 0.30 and queue[-1]["confidence"] == 0.95
    assert queue[-1]["employee_id"] == "E-04" and queue[-1]["hours"] == 80


def test_risk_is_rescored_each_plan(router, tools, clock):
    router.plan([])
    before = router.risk[E01]
    tools.send_message(E01, "M-01", "email", "Please submit.")
    clock.advance(timedelta(minutes=30))
    router.plan([])
    assert router.risk[E01] > before
