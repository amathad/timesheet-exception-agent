import pytest

from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.seed import seed


@pytest.fixture(scope="module")
def conn():
    with get_conn() as c:
        seed(c)
        yield c


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def test_row_counts(conn):
    assert scalar(conn, "SELECT count(*) FROM employees") == 17
    assert scalar(conn, "SELECT count(*) FROM exceptions") == 8
    assert scalar(conn, "SELECT count(*) FROM pay_periods") == 4


def test_every_persona_is_used(conn):
    personas = {r[0] for r in conn.execute("SELECT persona FROM sim_personas")}
    assert personas == {"responsive", "on_leave", "slow", "correction", "silent", "no_next_level"}


def test_no_next_level_manager_has_no_manager(conn):
    manager = scalar(
        conn,
        "SELECT e.manager_id FROM employees e JOIN sim_personas p ON p.manager_id = e.id"
        " WHERE p.persona = 'no_next_level'",
    )
    assert manager is None


def test_on_leave_managers_show_out_of_office(conn):
    rows = conn.execute(
        "SELECT pr.status FROM presence pr JOIN sim_personas p ON p.manager_id = pr.person_id"
        " WHERE p.persona IN ('on_leave', 'no_next_level')"
    ).fetchall()
    assert rows and all(r[0] == "out_of_office" for r in rows)


def test_current_period_locks_monday_10am_mountain(conn):
    local = scalar(
        conn,
        "SELECT to_char(lock_at AT TIME ZONE 'America/Denver', 'Dy HH24:MI')"
        " FROM pay_periods ORDER BY start_date DESC LIMIT 1",
    )
    assert local == "Mon 10:00"


def test_new_hire_has_no_history(conn):
    assert scalar(conn, "SELECT count(*) FROM timesheet_history WHERE employee_id = 'E-06'") == 0


def test_exceptions_all_on_current_period(conn):
    periods = scalar(conn, "SELECT count(DISTINCT pay_period_id) FROM exceptions")
    current = scalar(conn, "SELECT max(id) FROM pay_periods")
    assert periods == 1
    assert scalar(conn, "SELECT min(pay_period_id) FROM exceptions") == current
