"""Load a small, fixed synthetic dataset for one coordinator and one state.

Every name and email is invented. Re-running wipes the simulation tables and reloads the
same rows, so scenario tests always start from a known state. The retrieval tables
(policy_chunks, case_memory) are left alone; retrieval/ingest.py loads them.

Run: uv run python -m timesheet_exception_agent.sim.seed
"""

from pathlib import Path

from timesheet_exception_agent.db.connection import get_conn

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

STATE = "CO"
TZ = "America/Denver"
COORDINATOR_ID = "C-001"

# (id, name, job_type, pay_code, manager_id, hire_date)
# Next-level managers have no manager. M-05 is a frontline manager with no next level.
MANAGERS = [
    ("N-01", "Rowan Vale", "manager", "SAL", None, "2018-03-05"),
    ("N-02", "Indira Pell", "manager", "SAL", None, "2019-07-15"),
    ("M-01", "Casey Thorn", "manager", "SAL", "N-01", "2020-01-13"),
    ("M-02", "Devon Marsh", "manager", "SAL", "N-01", "2021-04-19"),
    ("M-03", "Harper Quill", "manager", "SAL", "N-02", "2019-11-04"),
    ("M-04", "Jordan Fenwick", "manager", "SAL", "N-01", "2022-02-28"),
    ("M-05", "Sasha Lindqvist", "manager", "SAL", None, "2020-09-21"),
]

STAFF = [
    ("E-01", "Avery Brook", "full_time_hourly", "REG", "M-01", "2023-05-01"),
    ("E-02", "Blake Corran", "full_time_hourly", "REG", "M-01", "2022-08-15"),
    ("E-03", "Cameron Ashby", "full_time_hourly", "REG", "M-02", "2021-10-04"),
    ("E-04", "Dakota Reyes", "full_time_hourly", "REG", "M-03", "2023-01-09"),
    ("E-05", "Emerson Tal", "full_time_hourly", "REG", "M-04", "2024-03-18"),
    ("E-06", "Finley Osei", "full_time_hourly", "REG", "M-05", "2026-10-12"),  # new hire, no history
    ("E-07", "Gray Nakamura", "full_time_hourly", "REG", "M-03", "2022-06-27"),  # schedule change
    ("E-08", "Hollis Varga", "part_time_hourly", "REG", "M-02", "2024-09-30"),
    ("E-09", "Jules Okafor", "full_time_hourly", "REG", "M-05", "2021-02-01"),  # teammate of E-06
    ("E-10", "Kai Ferreira", "full_time_hourly", "REG", "M-03", "2023-07-24"),
]

PERSONAS = {
    "N-01": "responsive",
    "N-02": "slow",
    "M-01": "responsive",
    "M-02": "on_leave",
    "M-03": "silent",
    "M-04": "correction",
    "M-05": "no_next_level",
}

OUT_OF_OFFICE = {"M-02", "M-05"}

# Biweekly, oldest first. The last one is the period being worked: it locks Monday 10:00 AM.
# (start, end, lock_at local time, process_on)
PAY_PERIODS = [
    ("2026-08-31", "2026-09-13", "2026-09-14 10:00", "2026-09-15"),
    ("2026-09-14", "2026-09-27", "2026-09-28 10:00", "2026-09-29"),
    ("2026-09-28", "2026-10-11", "2026-10-12 10:00", "2026-10-13"),
    ("2026-10-12", "2026-10-25", "2026-10-26 10:00", "2026-10-27"),
]

# Hours for the three closed periods, oldest first. E-06 was hired into the current period.
HISTORY = {
    "E-01": [80, 80, 80],
    "E-02": [80, 76, 80],
    "E-03": [80, 80, 80],
    "E-04": [80, 80, 80],
    "E-05": [80, 80, 80],
    "E-07": [80, 80, 64],  # last two periods disagree
    "E-08": [48, 48, 48],
    "E-09": [80, 80, 80],
    "E-10": [72, 80, 80],
}

# Exceptions on the current period: (employee_id, type)
EXCEPTIONS = [
    ("E-01", "not_submitted"),  # responsive manager
    ("E-02", "not_approved"),  # responsive manager
    ("E-03", "not_approved"),  # manager on leave -> responsive next level
    ("E-08", "not_submitted"),  # manager on leave -> responsive next level
    ("E-04", "not_submitted"),  # silent manager -> slow next level -> history fallback
    ("E-07", "not_approved"),  # silent manager, conflicting history
    ("E-05", "unusual_hours"),  # manager replies with a correction
    ("E-06", "not_submitted"),  # no next level, no history -> team, then standard hours
]

TABLES = [
    "audit_log", "messages", "sim_personas", "manager_stats", "presence",
    "exceptions", "timesheet_history", "pay_periods", "coordinator_states", "employees",
]


def email(name: str) -> str:
    return name.lower().replace(" ", ".") + "@example.com"


def seed(conn) -> None:
    conn.execute(SCHEMA.read_text())
    conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")

    # Managers first so manager_id references resolve.
    for emp_id, name, job_type, pay_code, manager_id, hire_date in MANAGERS + STAFF:
        conn.execute(
            "INSERT INTO employees (id, name, email, state, job_type, pay_code, manager_id, hire_date)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (emp_id, name, email(name), STATE, job_type, pay_code, manager_id, hire_date),
        )

    conn.execute(
        "INSERT INTO coordinator_states (coordinator_id, state) VALUES (%s, %s)",
        (COORDINATOR_ID, STATE),
    )

    period_ids = []
    for start, end, lock_local, process_on in PAY_PERIODS:
        row = conn.execute(
            "INSERT INTO pay_periods (start_date, end_date, lock_at, process_on)"
            " VALUES (%s, %s, (%s::timestamp AT TIME ZONE %s), %s) RETURNING id",
            (start, end, lock_local, TZ, process_on),
        ).fetchone()
        period_ids.append(row[0])
    current_period = period_ids[-1]

    for emp_id, hours in HISTORY.items():
        for period_id, h in zip(period_ids[:-1], hours):
            conn.execute(
                "INSERT INTO timesheet_history (employee_id, pay_period_id, hours, pay_code)"
                " VALUES (%s, %s, %s, 'REG')",
                (emp_id, period_id, h),
            )

    for emp_id, ex_type in EXCEPTIONS:
        conn.execute(
            "INSERT INTO exceptions (pay_period_id, employee_id, type) VALUES (%s, %s, %s)",
            (current_period, emp_id, ex_type),
        )

    # Presence as of the report being ready (Sunday evening before lock).
    for emp_id, *_ in MANAGERS:
        status = "out_of_office" if emp_id in OUT_OF_OFFICE else "available"
        conn.execute(
            "INSERT INTO presence (person_id, status, updated_at)"
            " VALUES (%s, %s, ('2026-10-25 18:00'::timestamp AT TIME ZONE %s))",
            (emp_id, status, TZ),
        )

    for manager_id, persona in PERSONAS.items():
        conn.execute(
            "INSERT INTO sim_personas (manager_id, persona) VALUES (%s, %s)",
            (manager_id, persona),
        )


if __name__ == "__main__":
    with get_conn() as conn:
        seed(conn)
        for table in reversed(TABLES):
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"{table:20} {count}")
