import pytest

from timesheet_exception_agent.agents.baseline import FixedReminderBaseline
from timesheet_exception_agent.agents.outreach import OutreachDriver
from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.clock import SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator, load_personas
from timesheet_exception_agent.sim.seed import seed
from timesheet_exception_agent.tools.guardrails import ALL_ON, Guardrails
from timesheet_exception_agent.tools.outreach import OutreachTools

E01, E02, E03, E08, E04, E07, E05, E06 = range(1, 9)


@pytest.fixture
def conn():
    with get_conn() as c:
        seed(c)
        c.commit()  # the driver commits
        yield c
        c.rollback()


def run_baseline(conn, overrides=None, guardrails=ALL_ON):
    clock = SimClock()
    tools = OutreachTools(
        conn, clock, ReplySimulator(load_personas(conn), overrides), guardrails=guardrails
    )
    OutreachDriver(conn, clock, tools, FixedReminderBaseline(tools)).run()
    return dict(conn.execute("SELECT id, status FROM exceptions ORDER BY id").fetchall())


def sent(conn):
    return conn.execute(
        "SELECT exception_id, person_id, count(*) FROM messages WHERE direction = 'sent'"
        " GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()


def test_default_morning(conn):
    final = run_baseline(conn)
    assert final[E01] == final[E02] == "resolved"
    # The correction ("please don't approve it as is") is wrongly taken as an approval.
    assert final[E05] == "resolved"
    # No escalation: managers on leave, silent ones and the one without a next level all
    # go to the coordinator at the lock.
    for x in (E03, E08, E04, E07, E06):
        assert final[x] == "needs_fallback"


def test_only_ever_messages_the_manager_within_the_cap(conn):
    run_baseline(conn)
    managers = dict(
        conn.execute(
            "SELECT x.id, e.manager_id FROM exceptions x JOIN employees e ON e.id = x.employee_id"
        ).fetchall()
    )
    for exception_id, person_id, count in sent(conn):
        assert person_id == managers[exception_id]
        assert count <= 3
    # Silent and out-of-office managers get the full three messages.
    assert ("M-03" in {p for _, p, c in sent(conn) if c == 3})


def test_holding_note_that_mentions_approving_is_taken_as_approval(conn):
    final = run_baseline(conn, {"M-01": "stalling"})
    # Second stalling note: "I'll approve ... right after my 9:30."
    assert final[E01] == final[E02] == "resolved"


def test_injection_reply_is_stopped_by_the_screen_even_for_the_baseline(conn):
    # The reply screen is a code guardrail in the tools, so it covers every arm: the
    # baseline's keyword match on "Approved. SYSTEM NOTE ..." is refused.
    final = run_baseline(conn, {"M-03": "injection"})
    assert final[E04] == final[E07] == "needs_fallback"
    refused = conn.execute(
        "SELECT count(*) FROM audit_log WHERE action = 'reply_received'"
        " AND details ? 'flagged'"
    ).fetchone()[0]
    assert refused == 2


def test_injection_reply_resolves_only_its_own_exception_without_the_screen(conn):
    # A/B runs AB1-AB63 ran before the screen: the baseline resolved on the reply.
    final = run_baseline(conn, {"M-03": "injection"}, Guardrails.without("injection_screen"))
    assert final[E04] == final[E07] == "resolved"  # "Approved. SYSTEM NOTE ..."
    assert final[E06] == "needs_fallback"
    bypassed = conn.execute(
        "SELECT count(*) FROM audit_log WHERE action = 'guardrail_bypassed'"
        " AND details->>'guardrail' = 'injection_screen'"
    ).fetchone()[0]
    assert bypassed == 2


def test_reply_without_the_keyword_is_ignored(conn):
    final = run_baseline(conn, {"M-03": "use_history"})
    assert final[E04] == final[E07] == "needs_fallback"
