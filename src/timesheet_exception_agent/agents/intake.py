"""Intake / triage (Module 5, code only): load, dedupe, group and score exceptions.

Runs once when the lock morning starts, before any outreach:
- Load the exceptions on the period being worked for the coordinator's states.
- Dedupe: a second exception for the same employee and period is marked 'duplicate'
  (pointing at the one kept, the lowest id) and is never worked.
- Group by manager, so the router can apply what it learns about a manager to all of
  their exceptions.
- Score lock risk (forecasting element 1): the chance an exception reaches the 10:00 AM
  lock without a manager decision.

The router calls `score` again every tick, since risk changes with status, replies and the
time left.

Risk uses only facts the agents may see: presence, the escalation chain, pay history,
manager_stats, messages and the clock. It never reads sim_personas. The weights are
placeholders, like the Estimator's confidence values: the feedback loop is meant to tune
them from real outcomes across pay periods. Every term is stored in triage.factors, so a
score can always be explained.

Every write is audited as actor 'intake'. The caller owns the transaction.
"""

from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from timesheet_exception_agent.sim.clock import LOCK, START, SimClock
from timesheet_exception_agent.sim.seed import COORDINATOR_ID
from timesheet_exception_agent.tools.retrieval import history_check

ACTOR = "intake"

# Placeholder weights (2026-11-16), to be tuned by the feedback loop.
BASE = 0.10
STATIC = {
    "manager_out_of_office": 0.30,  # first manager can't act; depends on the next level
    "no_next_level": 0.20,  # nobody to escalate to
    "history_unreliable": 0.10,  # a hand-off would need a teammate or standard hours
    "slow_manager": 0.15,  # manager_stats: average reply of 30 minutes or more
}
NO_REPLY_30 = 0.15  # contacted 30+ minutes ago and nobody has replied
ESCALATED = 0.15  # already past the first manager
TIME_PRESSURE = 0.25  # scaled by how much of the 8:00-10:00 window has gone
SLOW_REPLY_MINUTES = 30
MAX_WAITING = 0.99  # a waiting exception is never certain to miss

WAITING = ("open", "contacted", "escalated")
DECIDED = ("resolved", "correction_requested", "closed", "duplicate")


@dataclass(frozen=True)
class RiskFacts:
    status: str
    manager_out: bool
    has_next_level: bool
    recent_hours: list[float]  # newest first, last two closed periods
    avg_reply_minutes: float | None  # manager_stats, None when there is no data yet
    first_sent_at: datetime | None
    replies: int


def lock_risk(facts: RiskFacts, now: datetime) -> tuple[float, dict]:
    """Lock risk in [0, 1] and the terms that make it up. Pure; no database."""
    if facts.status in DECIDED:
        return 0.0, {"decided": facts.status}
    if facts.status == "needs_fallback":
        return 1.0, {"handed_to_coordinator": True}
    factors = {"base": BASE}
    if facts.manager_out:
        factors["manager_out_of_office"] = STATIC["manager_out_of_office"]
    if not facts.has_next_level:
        factors["no_next_level"] = STATIC["no_next_level"]
    if history_check(facts.recent_hours) is not None:
        factors["history_unreliable"] = STATIC["history_unreliable"]
    if (
        facts.avg_reply_minutes is not None
        and facts.avg_reply_minutes >= SLOW_REPLY_MINUTES
    ):
        factors["slow_manager"] = STATIC["slow_manager"]
    if (
        facts.status == "contacted"
        and facts.replies == 0
        and facts.first_sent_at is not None
        and (now - facts.first_sent_at).total_seconds() >= 30 * 60
    ):
        factors["no_reply_30"] = NO_REPLY_30
    if facts.status == "escalated":
        factors["escalated"] = ESCALATED
    window = (LOCK - START).total_seconds()
    gone = min(max((now - START).total_seconds() / window, 0.0), 1.0)
    if gone:
        factors["time_pressure"] = round(TIME_PRESSURE * gone, 3)
    risk = min(round(sum(factors.values()), 2), MAX_WAITING)
    return risk, factors


class Intake:
    def __init__(self, conn, clock: SimClock, coordinator_id: str = COORDINATOR_ID):
        self.conn = conn
        self.clock = clock
        self.coordinator_id = coordinator_id

    def run(self) -> dict:
        """Load, dedupe and score the period's exceptions. Returns a short summary."""
        rows = self.conn.execute(
            "SELECT x.id, x.employee_id, e.manager_id, x.status FROM exceptions x"
            " JOIN employees e ON e.id = x.employee_id"
            " WHERE x.pay_period_id = (SELECT max(id) FROM pay_periods)"
            " AND e.state IN (SELECT state FROM coordinator_states"
            "                 WHERE coordinator_id = %s)"
            " ORDER BY x.id",
            (self.coordinator_id,),
        ).fetchall()
        kept: dict[str, int] = {}
        duplicates: dict[int, int] = {}
        now = self.clock.now()
        for exception_id, employee_id, manager_id, status in rows:
            original = kept.setdefault(employee_id, exception_id)
            duplicate_of = None
            if original != exception_id and status == "open":
                duplicate_of = original
                duplicates[exception_id] = original
                self.conn.execute(
                    "UPDATE exceptions SET status = 'duplicate', next_check_at = NULL"
                    " WHERE id = %s",
                    (exception_id,),
                )
                self._audit(
                    "mark_duplicate",
                    exception_id,
                    f"same employee ({employee_id}) and period as exception {original},"
                    " which is worked instead",
                    {"duplicate_of": original, "employee_id": employee_id},
                )
            self.conn.execute(
                "INSERT INTO triage (exception_id, manager_id, risk, factors,"
                "  duplicate_of, updated_at) VALUES (%s, %s, 0, '{}', %s, %s)"
                " ON CONFLICT (exception_id) DO UPDATE SET manager_id = EXCLUDED.manager_id,"
                "  duplicate_of = EXCLUDED.duplicate_of, updated_at = EXCLUDED.updated_at",
                (exception_id, manager_id, duplicate_of, now),
            )
        scores = self.score()
        groups = self.groups()
        summary = {
            "loaded": len(rows),
            "duplicates": duplicates,
            "managers": len(groups),
            "risk": scores,
        }
        self._audit(
            "intake",
            None,
            f"loaded {len(rows)} exceptions, {len(duplicates)} duplicate,"
            f" {len(groups)} managers",
            {
                "duplicates": {str(k): v for k, v in duplicates.items()},
                "groups": groups,
                "risk": {str(k): v for k, v in scores.items()},
            },
        )
        return summary

    def groups(self) -> dict[str, list[int]]:
        """Exceptions still to be worked, by manager."""
        out: dict[str, list[int]] = {}
        for manager_id, exception_id in self.conn.execute(
            "SELECT t.manager_id, t.exception_id FROM triage t"
            " JOIN exceptions x ON x.id = t.exception_id"
            " WHERE x.status <> 'duplicate' ORDER BY t.manager_id, t.exception_id"
        ):
            out.setdefault(manager_id, []).append(exception_id)
        return out

    def score(self) -> dict[int, float]:
        """Recompute lock risk for every triaged exception; returns {exception_id: risk}.

        Not audited per tick (it would add a row per exception every 10 minutes); the
        current value and its factors are always in the triage table.
        """
        rows = self.conn.execute(
            "SELECT t.exception_id, x.status, x.employee_id, x.pay_period_id,"
            "  p.status = 'out_of_office', m.manager_id IS NOT NULL, s.avg_reply_minutes,"
            "  (SELECT min(sim_time) FROM messages WHERE exception_id = x.id"
            "     AND direction = 'sent'),"
            "  (SELECT count(*) FROM messages WHERE exception_id = x.id"
            "     AND direction = 'received')"
            " FROM triage t JOIN exceptions x ON x.id = t.exception_id"
            " LEFT JOIN employees m ON m.id = t.manager_id"
            " LEFT JOIN presence p ON p.person_id = t.manager_id"
            " LEFT JOIN manager_stats s ON s.manager_id = t.manager_id"
            " ORDER BY t.exception_id"
        ).fetchall()
        now = self.clock.now()
        out = {}
        for (
            exception_id,
            status,
            employee_id,
            period_id,
            manager_out,
            has_next,
            avg_reply,
            first_sent,
            replies,
        ) in rows:
            hours = [
                float(h)
                for (h,) in self.conn.execute(
                    "SELECT h.hours FROM timesheet_history h"
                    " JOIN pay_periods p ON p.id = h.pay_period_id"
                    " WHERE h.employee_id = %s AND p.id < %s"
                    " ORDER BY p.end_date DESC LIMIT 2",
                    (employee_id, period_id),
                )
            ]
            facts = RiskFacts(
                status=status,
                manager_out=bool(manager_out) or self._auto_replied(exception_id),
                has_next_level=bool(has_next),
                recent_hours=hours,
                avg_reply_minutes=float(avg_reply) if avg_reply is not None else None,
                first_sent_at=first_sent,
                replies=replies,
            )
            risk, factors = lock_risk(facts, now)
            self.conn.execute(
                "UPDATE triage SET risk = %s, factors = %s, updated_at = %s"
                " WHERE exception_id = %s",
                (risk, Jsonb(factors), now, exception_id),
            )
            out[exception_id] = risk
        return out

    def _auto_replied(self, exception_id: int) -> bool:
        """True when the manager has sent an out-of-office auto-reply on any exception."""
        return self.conn.execute(
            "SELECT EXISTS (SELECT 1 FROM messages r JOIN triage t"
            "   ON t.exception_id = %s AND r.person_id = t.manager_id"
            " WHERE r.direction = 'received' AND r.body LIKE 'Automatic reply%%')",
            (exception_id,),
        ).fetchone()[0]

    def _audit(self, action, exception_id, reason, details) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (sim_time, actor, action, exception_id, reason, details)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (self.clock.now(), ACTOR, action, exception_id, reason, Jsonb(details)),
        )
