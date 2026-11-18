"""Rule-based router (Module 5, code only): the Supervisor's fallback and its A/B baseline.

Each tick, after the driver delivers replies and lists the wakes, the router:
1. Rescores lock risk for every exception (Intake.score).
2. Applies manager-level facts across a manager's exceptions: once a manager is known to be
   out of office (Teams presence, or an out-of-office auto-reply on any of their
   exceptions), every other exception still waiting on that manager wakes now with
   `manager_out`, so the agent escalates it instead of waiting for its own follow-up timer.
   Each exception gets this wake once.
3. Orders the wakes by risk, highest first (exception id breaks ties).

At the lock it builds the coordinator's approval queue from the Estimator's proposals,
lowest confidence first, since those need the most attention.

The LLM Supervisor (planned) makes the same kinds of decisions; it is kept only if it beats
this router on the same scenarios. Every manager_out wake is audited as actor 'router'.
"""

from psycopg.types.json import Jsonb

from timesheet_exception_agent.agents.intake import Intake
from timesheet_exception_agent.agents.outreach import Wake
from timesheet_exception_agent.sim.clock import SimClock

ACTOR = "router"

# Wakes the router may replace with manager_out (the agent learns more from manager_out).
REPLACEABLE = ("follow_up_due",)


class Router:
    def __init__(self, conn, clock: SimClock, intake: Intake) -> None:
        self.conn = conn
        self.clock = clock
        self.intake = intake
        self.out_woken: set[int] = set()
        self.risk: dict[int, float] = {}

    def managers_out(self) -> set[str]:
        """Managers known to be out: presence out_of_office, or an auto-reply received."""
        return {
            person_id
            for (person_id,) in self.conn.execute(
                "SELECT person_id FROM presence WHERE status = 'out_of_office'"
                " UNION SELECT DISTINCT r.person_id FROM messages r"
                " JOIN triage t ON t.manager_id = r.person_id"
                " WHERE r.direction = 'received' AND r.body LIKE 'Automatic reply%%'"
            )
        }

    def plan(self, wakes: list[Wake]) -> list[Wake]:
        """The driver's wakes for this tick, plus manager_out wakes, in risk order."""
        self.risk = self.intake.score()
        by_id = {w.exception_id: w for w in wakes}
        out = self.managers_out()
        rows = self.conn.execute(
            "SELECT x.id, t.manager_id FROM exceptions x"
            " JOIN triage t ON t.exception_id = x.id"
            " WHERE x.status = 'contacted' ORDER BY x.id"
        ).fetchall()
        for exception_id, manager_id in rows:
            if manager_id not in out or exception_id in self.out_woken:
                continue
            current = by_id.get(exception_id)
            if current is not None and current.reason not in REPLACEABLE:
                continue  # a new reply or the cut-off wake already covers it
            self.out_woken.add(exception_id)
            by_id[exception_id] = Wake(exception_id, "manager_out")
            self._audit(
                "route_manager_out",
                exception_id,
                f"manager {manager_id} is out of office (presence or auto-reply);"
                " wake to escalate now",
                {"manager_id": manager_id, "replaced": current.reason if current else None},
            )
        return sorted(
            by_id.values(),
            key=lambda w: (-self.risk.get(w.exception_id, 0.0), w.exception_id),
        )

    def approval_queue(self) -> list[dict]:
        """Proposals waiting for the coordinator, lowest confidence first."""
        rows = self.conn.execute(
            "SELECT s.exception_id, x.employee_id, e.name, s.path, s.hours, s.pay_code,"
            "  s.confidence, s.reason, s.flag, s.runner_up FROM estimates s"
            " JOIN exceptions x ON x.id = s.exception_id"
            " JOIN employees e ON e.id = x.employee_id"
            " WHERE s.status = 'proposed' AND x.status = 'needs_fallback'"
            " ORDER BY s.confidence, s.exception_id"
        ).fetchall()
        return [
            {
                "exception_id": exception_id,
                "employee_id": employee_id,
                "employee": name,
                "path": path,
                "hours": float(hours),
                "pay_code": code,
                "confidence": float(confidence),
                "reason": reason,
                "flag": flag,
                "runner_up": runner_up,
            }
            for (
                exception_id, employee_id, name, path, hours, code, confidence, reason, flag,
                runner_up,
            ) in rows
        ]

    def _audit(self, action, exception_id, reason, details) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (sim_time, actor, action, exception_id, reason, details)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (self.clock.now(), ACTOR, action, exception_id, reason, Jsonb(details)),
        )
