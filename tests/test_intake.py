from datetime import timedelta

import pytest

from timesheet_exception_agent.agents.intake import (
    ACTOR,
    BASE,
    MAX_WAITING,
    STATIC,
    Intake,
    RiskFacts,
    lock_risk,
)
from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.clock import LOCK, START, SimClock
from timesheet_exception_agent.sim.seed import add_duplicate, seed

E01, E02, E03, E08, E04, E07, E05, E06 = range(1, 9)


def facts(**kw):
    base = {
        "status": "open",
        "manager_out": False,
        "has_next_level": True,
        "recent_hours": [80, 80],
        "avg_reply_minutes": None,
        "first_sent_at": None,
        "replies": 0,
    }
    return RiskFacts(**{**base, **kw})


# Pure scoring


def test_quiet_exception_at_start_has_base_risk_only():
    risk, factors = lock_risk(facts(), START)
    assert risk == BASE and factors == {"base": BASE}


def test_static_factors_add_up():
    risk, factors = lock_risk(
        facts(manager_out=True, has_next_level=False, recent_hours=[]), START
    )
    assert set(factors) == {"base", "manager_out_of_office", "no_next_level", "history_unreliable"}
    assert risk == pytest.approx(0.70)


def test_slow_manager_from_manager_stats():
    _, factors = lock_risk(facts(avg_reply_minutes=45), START)
    assert factors["slow_manager"] == STATIC["slow_manager"]
    _, factors = lock_risk(facts(avg_reply_minutes=10), START)
    assert "slow_manager" not in factors


def test_no_reply_after_thirty_minutes_raises_risk():
    sent = START
    _, early = lock_risk(facts(status="contacted", first_sent_at=sent), START + timedelta(minutes=20))
    _, late = lock_risk(facts(status="contacted", first_sent_at=sent), START + timedelta(minutes=30))
    assert "no_reply_30" not in early and "no_reply_30" in late
    _, replied = lock_risk(
        facts(status="contacted", first_sent_at=sent, replies=1), START + timedelta(minutes=30)
    )
    assert "no_reply_30" not in replied


def test_time_pressure_grows_toward_the_lock_and_is_capped():
    r_start, _ = lock_risk(facts(), START)
    r_mid, _ = lock_risk(facts(), START + timedelta(minutes=60))
    r_lock, _ = lock_risk(facts(), LOCK)
    assert r_start < r_mid < r_lock
    worst, _ = lock_risk(
        facts(status="escalated", manager_out=True, has_next_level=False, recent_hours=[],
              avg_reply_minutes=60),
        LOCK,
    )
    assert worst == MAX_WAITING


@pytest.mark.parametrize("status", ["resolved", "correction_requested", "closed", "duplicate"])
def test_decided_exceptions_have_no_risk(status):
    assert lock_risk(facts(status=status), LOCK)[0] == 0.0


def test_handed_off_exception_has_missed_the_manager():
    assert lock_risk(facts(status="needs_fallback"), START)[0] == 1.0


# Database (rolled back)


@pytest.fixture
def conn():
    with get_conn() as c:
        seed(c)
        yield c
        c.rollback()


@pytest.fixture
def intake(conn):
    return Intake(conn, SimClock())


def test_seed_risk_at_start(intake):
    summary = intake.run()
    risk = summary["risk"]
    assert summary["loaded"] == 8 and summary["duplicates"] == {}
    assert risk[E01] == risk[E04] == risk[E05] == 0.10
    assert risk[E03] == risk[E08] == 0.40  # M-02 out of office
    assert risk[E07] == 0.20  # history 80 then 64
    assert risk[E02] == 0.20  # history 76 then 80
    assert risk[E06] == 0.70  # M-05 out, no next level, new hire with no history
    assert max(risk, key=risk.get) == E06


def test_groups_by_manager(intake):
    intake.run()
    assert intake.groups() == {
        "M-01": [E01, E02],
        "M-02": [E03, E08],
        "M-03": [E04, E07],
        "M-04": [E05],
        "M-05": [E06],
    }


def test_duplicate_is_marked_and_kept_out_of_groups(conn, intake):
    dup = add_duplicate(conn)
    summary = intake.run()
    assert summary["duplicates"] == {dup: E01}
    status = conn.execute("SELECT status FROM exceptions WHERE id = %s", (dup,)).fetchone()[0]
    assert status == "duplicate"
    assert dup not in intake.groups()["M-01"]
    assert conn.execute(
        "SELECT duplicate_of FROM triage WHERE exception_id = %s", (dup,)
    ).fetchone()[0] == E01
    actions = conn.execute(
        "SELECT action, exception_id FROM audit_log WHERE actor = %s ORDER BY id", (ACTOR,)
    ).fetchall()
    assert actions == [("mark_duplicate", dup), ("intake", None)]


def test_risk_never_reads_sim_personas(conn, intake):
    before = intake.run()["risk"]
    conn.execute("UPDATE sim_personas SET persona = 'silent'")
    assert intake.score() == before


def test_auto_reply_on_one_exception_marks_the_manager_out_everywhere(conn, intake):
    intake.run()
    conn.execute("UPDATE presence SET status = 'available' WHERE person_id = 'M-02'")
    assert intake.score()[E08] == 0.10
    conn.execute(
        "INSERT INTO messages (exception_id, person_id, channel, direction, body, sim_time)"
        " VALUES (%s, 'M-02', 'email', 'received', 'Automatic reply: out today', %s)",
        (E03, START),
    )
    assert intake.score()[E08] == 0.40


def test_manager_stats_feed_the_score(conn, intake):
    intake.run()
    conn.execute(
        "INSERT INTO manager_stats (manager_id, avg_reply_minutes, updated_at)"
        " VALUES ('M-01', 50, now())"
    )
    assert intake.score()[E01] == 0.25
