"""Coordinator approval gate (Module 5): the only place fallback time is approved.

The agents only propose. At the lock, the graph pauses with the approval queue, and the
coordinator answers each proposal:
- approve: enter the proposed hours and pay code.
- edit: enter different hours (and optionally a pay code); the proposal is kept as it was.
- reject: enter nothing now; the exception stays with the coordinator (needs_fallback).

Approve and edit close the exception. Every decision records the coordinator's initials and
the time (estimates.approved_by / approved_at) and is audited as actor 'coordinator'.
Decisions are validated in code; nothing here is ever called by a model.
"""

import re
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from timesheet_exception_agent.sim.clock import SimClock

ACTOR = "coordinator"
DECISIONS = ("approve", "edit", "reject")
INITIALS = re.compile(r"^[A-Z]{2,3}$")
MAX_HOURS = 200


class ApprovalError(Exception):
    """A decision the gate refuses (unknown exception, bad initials, bad hours...)."""


@dataclass(frozen=True)
class Decision:
    exception_id: int
    decision: str  # approve | edit | reject
    initials: str
    hours: float | None = None  # edit only
    pay_code: str | None = None  # edit only; defaults to the proposal's
    note: str = ""


def parse_decision(item: dict) -> Decision:
    return Decision(
        exception_id=int(item["exception_id"]),
        decision=str(item["decision"]).strip().lower(),
        initials=str(item.get("initials", "")).strip().upper(),
        hours=None if item.get("hours") is None else float(item["hours"]),
        pay_code=item.get("pay_code"),
        note=str(item.get("note", "")),
    )


class ApprovalGate:
    def __init__(self, conn, clock: SimClock) -> None:
        self.conn = conn
        self.clock = clock

    def apply(self, d: Decision) -> dict:
        if d.decision not in DECISIONS:
            raise ApprovalError(f"decision must be one of {DECISIONS}")
        if not INITIALS.match(d.initials):
            raise ApprovalError("initials must be 2 or 3 letters")
        row = self.conn.execute(
            "SELECT s.status, s.hours, s.pay_code, x.status FROM estimates s"
            " JOIN exceptions x ON x.id = s.exception_id WHERE s.exception_id = %s",
            (d.exception_id,),
        ).fetchone()
        if row is None:
            raise ApprovalError(f"no proposal for exception {d.exception_id}")
        est_status, hours, code, x_status = row
        if est_status != "proposed" or x_status != "needs_fallback":
            raise ApprovalError(
                f"exception {d.exception_id} is not waiting for approval"
                f" (proposal {est_status}, exception {x_status})"
            )
        now = self.clock.now()
        final_hours, final_code = None, None
        if d.decision == "approve":
            final_hours, final_code = float(hours), code
        elif d.decision == "edit":
            if d.hours is None or not 0 <= d.hours <= MAX_HOURS:
                raise ApprovalError(f"edit needs hours between 0 and {MAX_HOURS}")
            final_hours, final_code = d.hours, (d.pay_code or code)
        status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[
            d.decision
        ]
        self.conn.execute(
            "UPDATE estimates SET status = %s, approved_by = %s, approved_at = %s,"
            " final_hours = %s, final_pay_code = %s WHERE exception_id = %s",
            (status, d.initials, now, final_hours, final_code, d.exception_id),
        )
        if d.decision != "reject":
            self.conn.execute(
                "UPDATE exceptions SET status = 'closed', next_check_at = NULL"
                " WHERE id = %s",
                (d.exception_id,),
            )
        details = {
            "initials": d.initials,
            "decision": d.decision,
            "proposed_hours": float(hours),
            "proposed_pay_code": code,
            "final_hours": final_hours,
            "final_pay_code": final_code,
        }
        self.conn.execute(
            "INSERT INTO audit_log (sim_time, actor, action, exception_id, reason, details)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                now,
                ACTOR,
                f"{d.decision}_estimate",
                d.exception_id,
                d.note or f"coordinator {d.initials}: {d.decision}",
                Jsonb(details),
            ),
        )
        return {"exception_id": d.exception_id, "estimate": status, **details}
