"""Estimator: fallback hour proposals for exceptions handed off, with gated Tree-of-Thought.

When an exception reaches the coordinator (needs_fallback), the Estimator proposes the hours
to enter, with a pay code, a path and a confidence score. Every number comes from
deterministic code. With a ToTReasoner (agents/tot.py), an exception whose history fails the
check and that has two or more different candidates goes through a Tree-of-Thought: the
model judges the candidates code calculated (each period's hours, standard hours) against
the replies and similar past cases, and code picks the recommendation and a runner-up and
sets the confidence. Without a reasoner, or if the reasoning fails, the code proposal stands.
The Supervisor may send a ToT proposal back once with a concern (send_back).

Rules (from the policy handbook, decided in code):
- History path: the last two closed periods pass history_check (both exist, same hours)
  and have the same pay code. Propose those hours and that pay code.
  [history_fallback > What to copy from the last two periods]
- Teammate path: history fails the check. Ask a teammate first; the proposal carries
  standard hours for the job type as the backup if nobody confirms by end of day Monday.
  [history_fallback > When history is missing or unreliable]
  [team_and_standard_hours > Asking a teammate]
- Teammates are the manager's other direct reports, except anyone with an open exception of
  their own this period: their own time is in question, so they can't vouch for others'.
- Standard-hours path: history fails and no teammate can be asked.
- Standard hours: full-time hourly 8 h a day, part-time hourly 4 h a day (no schedule on
  file), prorated over weekdays from the hire date.
  [team_and_standard_hours > Standard hours by job type]

The Estimator only proposes. It never approves, never changes an exception's status and
never writes timesheet_history; approval goes through the Supervisor's gate (Week 5).
Every proposal, withdrawal and skip is written to audit_log as actor 'estimator'.

The caller owns the transaction: the Estimator never commits.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from psycopg.types.json import Jsonb

from timesheet_exception_agent.agents.tot import (
    MAX_EVIDENCE_TEXT,
    Candidate,
    ToTReasoner,
    ToTResult,
    build_candidates,
    gate,
)
from timesheet_exception_agent.retrieval.search import citation
from timesheet_exception_agent.sim.clock import SimClock
from timesheet_exception_agent.tools.outreach import ToolError
from timesheet_exception_agent.tools.retrieval import RetrievalTools, history_check

ACTOR = "estimator"

COPY_RULE = citation("history_fallback", "What to copy from the last two periods")
TEAMMATE_RULE = citation("team_and_standard_hours", "Asking a teammate")
STANDARD_RULE = citation("team_and_standard_hours", "Standard hours by job type")

# Standard hours per weekday by job type (team_and_standard_hours policy). Part-time uses
# 4 h a day because the seed has no scheduled hours on file.
HOURS_PER_DAY = {"full_time_hourly": 8.0, "part_time_hourly": 4.0}

# Confidence placeholders (agreed 2026-11-11), to be tuned by the feedback loop later.
CONFIDENCE_HISTORY_CONFIRMED = 0.95  # last two agree, and the period before agrees too
CONFIDENCE_HISTORY = 0.90  # last two agree, no third period on file
CONFIDENCE_HISTORY_THIRD_DIFFERS = 0.80  # last two agree, the period before differs
CONFIDENCE_STANDARD = 0.30  # history fails the check: standard hours

PERIODS_READ = 3  # the last two decide; the third only adjusts confidence

SPLIT_FLAG = (
    "Tree-of-Thought reasoning was split between the recommendation and the runner-up;"
    " check both before approving."
)


class EstimateError(Exception):
    """The Estimator can't propose for this exception (wrong status, unknown job type...)."""


@dataclass(frozen=True)
class Period:
    period_id: int
    end_date: date
    hours: float
    pay_code: str


@dataclass(frozen=True)
class Facts:
    exception_id: int
    status: str
    employee_id: str
    job_type: str
    pay_code: str  # the employee's pay code on file, used for standard hours
    hire_date: date
    period_start: date
    period_end: date
    history: list[Period]  # closed periods before the current one, newest first
    teammates: list[str] = field(default_factory=list)  # who can be asked
    # Teammates left out because they have an open exception of their own this period.
    teammates_excluded: list[str] = field(default_factory=list)
    # Teammates already messaged about this exception (by Outreach), from `messages`.
    teammates_contacted: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Proposal:
    path: str  # history | teammate | standard_hours
    method: str  # history | standard_hours
    hours: float
    pay_code: str
    confidence: float
    reason: str
    basis: dict
    runner_up: dict | None = None  # ToT only: the second-best candidate
    tot: dict | None = None  # ToT only: the reasoning record


# Pure rules


def pay_code_check(periods: list[Period]) -> str | None:
    """Why the last two periods' pay codes can't be copied, or None if they agree."""
    if len(periods) >= 2 and periods[0].pay_code != periods[1].pay_code:
        return (
            f"The last two periods use different pay codes ({periods[1].pay_code}, then"
            f" {periods[0].pay_code}), so history should not be copied. {COPY_RULE}"
        )
    return None


def history_confidence(periods: list[Period]) -> float:
    """Confidence for copying the last two periods, which must already pass the checks."""
    if len(periods) < 3:
        return CONFIDENCE_HISTORY
    newest, third = periods[0], periods[2]
    if third.hours == newest.hours and third.pay_code == newest.pay_code:
        return CONFIDENCE_HISTORY_CONFIRMED
    return CONFIDENCE_HISTORY_THIRD_DIFFERS


def weekdays(start: date, end: date) -> int:
    """Monday-to-Friday days from start to end, both included."""
    days, day = 0, start
    while day <= end:
        if day.weekday() < 5:
            days += 1
        day += timedelta(days=1)
    return days


def standard_hours(
    job_type: str, hire_date: date, period_start: date, period_end: date
) -> tuple[float, int]:
    """Standard hours for the period and the weekdays they cover, prorated from hire."""
    if job_type not in HOURS_PER_DAY:
        raise EstimateError(f"no standard hours defined for job type {job_type!r}")
    if hire_date > period_end:
        raise EstimateError(f"hired {hire_date}, after the period ended {period_end}")
    days = weekdays(max(hire_date, period_start), period_end)
    return HOURS_PER_DAY[job_type] * days, days


def decide(
    facts: Facts,
    choice: Candidate | None = None,
    choice_confidence: float | None = None,
    tot_note: str = "",
) -> Proposal:
    """The fallback proposal for one exception. Deterministic; no database, no model.

    `choice` is a ToT recommendation (one of the candidates code built); it replaces
    standard hours as what to enter if no teammate confirms."""
    recent = facts.history[:2]
    note = history_check([p.hours for p in recent]) or pay_code_check(recent)
    basis = {
        "job_type": facts.job_type,
        "hire_date": facts.hire_date.isoformat(),
        "period": [facts.period_start.isoformat(), facts.period_end.isoformat()],
        "history_newest_first": [
            {**asdict(p), "end_date": p.end_date.isoformat()} for p in facts.history
        ],
        "history_note": note,
        "teammates": facts.teammates,
        "teammates_excluded": facts.teammates_excluded,
        "teammates_contacted": facts.teammates_contacted,
    }
    if note is None:
        hours, code = recent[0].hours, recent[0].pay_code
        return Proposal(
            path="history",
            method="history",
            hours=hours,
            pay_code=code,
            confidence=history_confidence(facts.history),
            reason=(
                f"Copy the last two closed periods: {hours:g} hours {code} in each."
                f" Proposal only; the coordinator approves. {COPY_RULE}"
            ),
            basis=basis,
        )

    hours, days = standard_hours(
        facts.job_type, facts.hire_date, facts.period_start, facts.period_end
    )
    per_day = HOURS_PER_DAY[facts.job_type]
    basis["standard_hours"] = {"weekdays": days, "hours_per_day": per_day}
    standard = (
        f"standard hours: {hours:g} hours {facts.pay_code} ({days} weekdays at"
        f" {per_day:g} hours). {STANDARD_RULE}"
    )
    method, pay_code, conf = "standard_hours", facts.pay_code, CONFIDENCE_STANDARD
    if choice is not None:
        hours, pay_code, method = choice.hours, choice.pay_code, choice.method
        conf = choice_confidence if choice_confidence is not None else conf
        standard = f"the recommendation, {choice.label}: {hours:g} hours {pay_code}."
    if facts.teammates_contacted:
        path = "teammate"
        step = (
            f" Teammate {', '.join(facts.teammates_contacted)} was already asked about"
            f" this exception; wait for their answer. {TEAMMATE_RULE} If nobody confirms"
            f" by end of day Monday, enter {standard}"
        )
    elif facts.teammates:
        path = "teammate"
        step = (
            f" Ask a teammate ({', '.join(facts.teammates)}) first. {TEAMMATE_RULE} If"
            f" nobody confirms by end of day Monday, enter {standard}"
        )
    else:
        path = "standard_hours"
        if facts.teammates_excluded:
            step = (
                f" No teammate can confirm ({', '.join(facts.teammates_excluded)} have open"
                f" exceptions of their own), so enter {standard}"
            )
        else:
            step = f" No teammate on file, so enter {standard}"
    return Proposal(
        path=path,
        method=method,
        hours=hours,
        pay_code=pay_code,
        confidence=conf,
        reason=note + step + tot_note + " Proposal only; the coordinator approves.",
        basis=basis,
    )


def with_tot(facts: Facts, result: ToTResult) -> Proposal:
    """The proposal with the ToT recommendation in place of standard hours."""
    rec, ru = result.recommended, result.runner_up
    note = f" Reasoned over {len(result.candidates)} candidates (Tree-of-Thought)."
    if ru is not None:
        note += f" Runner-up: {ru.label}, {ru.hours:g} hours {ru.pay_code}."
    if result.agrees is False:
        note += (
            " The reasoning was split between the two, so the policy-safe choice is"
            " proposed at minimum confidence; check both before approving."
        )
    note += f" {result.explanation}"
    p = decide(facts, rec, result.confidence, note)
    runner = (
        {
            "hours": ru.hours,
            "pay_code": ru.pay_code,
            "method": ru.method,
            "label": ru.label,
            "score": ru.final,
        }
        if ru is not None
        else None
    )
    return Proposal(**{**asdict(p), "runner_up": runner, "tot": result.record()})


# Database


class Estimator:
    def __init__(
        self,
        conn,
        clock: SimClock,
        reasoner: ToTReasoner | None = None,
        retrieval: RetrievalTools | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        # Gated ToT is on only with a reasoner. `retrieval` (actor 'estimator') supplies
        # similar past cases as evidence; without it the tree sees facts and replies only.
        self.reasoner = reasoner
        self.retrieval = retrieval
        self._skipped: set[int] = set()

    def load_facts(self, exception_id: int) -> Facts:
        row = self.conn.execute(
            "SELECT x.status, e.id, e.job_type, e.pay_code, e.hire_date, e.manager_id,"
            "       p.start_date, p.end_date"
            " FROM exceptions x JOIN employees e ON e.id = x.employee_id"
            " JOIN pay_periods p ON p.id = x.pay_period_id WHERE x.id = %s",
            (exception_id,),
        ).fetchone()
        if row is None:
            raise EstimateError(f"no exception with id {exception_id}")
        status, emp_id, job_type, pay_code, hired, manager_id, start, end = row
        history = [
            Period(pid, p_end, float(hours), code)
            for pid, p_end, hours, code in self.conn.execute(
                "SELECT h.pay_period_id, p.end_date, h.hours, h.pay_code"
                " FROM timesheet_history h JOIN pay_periods p ON p.id = h.pay_period_id"
                " WHERE h.employee_id = %s AND p.end_date < %s"
                " ORDER BY p.end_date DESC LIMIT %s",
                (emp_id, start, PERIODS_READ),
            )
        ]
        # Teammates: the manager's other direct reports, as in get_exception, minus anyone
        # with an open exception of their own this period (their own time is in question,
        # so they can't vouch for someone else's). Excluded ones are kept in the basis.
        teammates, excluded = [], []
        for t_id, has_open in self.conn.execute(
            "SELECT t.id, EXISTS (SELECT 1 FROM exceptions o WHERE o.employee_id = t.id"
            "   AND o.pay_period_id = (SELECT pay_period_id FROM exceptions WHERE id = %s)"
            "   AND o.status NOT IN ('resolved', 'closed', 'duplicate'))"
            " FROM employees t WHERE t.manager_id = %s AND t.id <> %s ORDER BY t.id",
            (exception_id, manager_id, emp_id),
        ):
            (excluded if has_open else teammates).append(t_id)
        # Teammates Outreach has already messaged about this exception, so the proposal
        # says to wait for their answer instead of asking again.
        contacted = [
            p_id
            for (p_id,) in self.conn.execute(
                "SELECT DISTINCT person_id FROM messages WHERE exception_id = %s"
                " AND direction = 'sent' AND person_id = ANY(%s::text[]) ORDER BY person_id",
                (exception_id, teammates),
            )
        ]
        return Facts(
            exception_id=exception_id,
            status=status,
            employee_id=emp_id,
            job_type=job_type,
            pay_code=pay_code,
            hire_date=hired,
            period_start=start,
            period_end=end,
            history=history,
            teammates=teammates,
            teammates_excluded=excluded,
            teammates_contacted=contacted,
        )

    def propose(self, exception_id: int, concern: str | None = None) -> Proposal:
        """Write (or replace) the proposal for a handed-off exception and audit it.

        Refuses exceptions not at needs_fallback, and proposals the coordinator has
        already acted on (approved, edited or rejected). `concern` is a Supervisor
        send-back, given to the Tree-of-Thought as evidence.
        """
        facts = self.load_facts(exception_id)
        if facts.status != "needs_fallback":
            raise EstimateError(
                f"exception {exception_id} is {facts.status}, not needs_fallback;"
                " the Estimator only proposes for exceptions handed to the coordinator"
            )
        existing = self.conn.execute(
            "SELECT status FROM estimates WHERE exception_id = %s", (exception_id,)
        ).fetchone()
        if existing and existing[0] not in ("proposed", "withdrawn"):
            raise EstimateError(
                f"exception {exception_id} already has an estimate marked {existing[0]}"
            )
        proposal = decide(facts)
        if self.reasoner is not None and proposal.path != "history":
            proposal = self._reason(facts, proposal, concern)
        self.conn.execute(
            "INSERT INTO estimates (exception_id, path, method, hours, pay_code, confidence,"
            "  reason, basis, runner_up, tot, status, proposed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'proposed', %s)"
            " ON CONFLICT (exception_id) DO UPDATE SET path = EXCLUDED.path,"
            "  method = EXCLUDED.method, hours = EXCLUDED.hours,"
            "  pay_code = EXCLUDED.pay_code, confidence = EXCLUDED.confidence,"
            "  reason = EXCLUDED.reason, basis = EXCLUDED.basis,"
            "  runner_up = EXCLUDED.runner_up, tot = EXCLUDED.tot, status = 'proposed',"
            "  proposed_at = EXCLUDED.proposed_at",
            (
                exception_id,
                proposal.path,
                proposal.method,
                proposal.hours,
                proposal.pay_code,
                proposal.confidence,
                proposal.reason,
                Jsonb(proposal.basis),
                Jsonb(proposal.runner_up) if proposal.runner_up else None,
                Jsonb(proposal.tot) if proposal.tot else None,
                self.clock.now(),
            ),
        )
        if proposal.tot and proposal.tot.get("agrees") is False:
            # Priority review (Checkpoint 6.1): the tree's own self-check disagreed.
            self.conn.execute(
                "UPDATE estimates SET flag = %s WHERE exception_id = %s AND flag IS NULL",
                (SPLIT_FLAG, exception_id),
            )
        self._audit(
            "propose_estimate",
            exception_id,
            proposal.reason,
            {
                "path": proposal.path,
                "method": proposal.method,
                "hours": proposal.hours,
                "pay_code": proposal.pay_code,
                "confidence": proposal.confidence,
                "replaced": existing is not None,
                "basis": proposal.basis,
                "runner_up": proposal.runner_up,
                "tot": proposal.tot is not None,
            },
        )
        return proposal

    def send_back(self, exception_id: int, concern: str) -> dict:
        """The Supervisor's one send-back: rerun the Tree-of-Thought with its concern.

        Allowed once per proposal, and only for a proposal the tree made. Returns whether
        the recommendation changed; if not, the caller flags it for the coordinator."""
        row = self.conn.execute(
            "SELECT status, tot IS NOT NULL, sent_back, hours, pay_code FROM estimates"
            " WHERE exception_id = %s",
            (exception_id,),
        ).fetchone()
        if row is None or row[0] != "proposed":
            raise EstimateError(f"exception {exception_id} has no proposal waiting")
        _, has_tot, sent_back, hours, code = row
        if not has_tot or self.reasoner is None:
            raise EstimateError("only a Tree-of-Thought proposal can be sent back")
        if sent_back:
            raise EstimateError(
                f"the proposal for exception {exception_id} was already sent back"
            )
        before = (float(hours), code)
        p = self.propose(exception_id, concern=concern)
        self.conn.execute(
            "UPDATE estimates SET sent_back = true WHERE exception_id = %s",
            (exception_id,),
        )
        changed = (p.hours, p.pay_code) != before
        self._audit(
            "estimate_sent_back",
            exception_id,
            concern,
            {"before": list(before), "after": [p.hours, p.pay_code], "changed": changed},
        )
        return {"changed": changed, "hours": p.hours, "pay_code": p.pay_code}

    # Tree-of-Thought

    def _reason(self, facts: Facts, proposal: Proposal, concern: str | None) -> Proposal:
        candidates = build_candidates(
            [(p.end_date.isoformat(), p.hours, p.pay_code) for p in facts.history],
            (proposal.hours, proposal.pay_code),
        )
        if not gate(candidates, history_reliable=False):
            return proposal
        result = self.reasoner.run(
            facts.exception_id, candidates, self._context(facts, proposal), concern
        )
        record = result.record()
        self._audit(
            "tot_reasoning",
            facts.exception_id,
            (
                f"recommended {result.recommended.label}"
                if result.recommended
                else "no candidate scored; the code proposal stands"
            ),
            record,
        )
        if result.recommended is None:
            return Proposal(**{**asdict(proposal), "tot": record})
        return with_tot(facts, result)

    def _context(self, facts: Facts, proposal: Proposal) -> str:
        """Evidence for the tree, gathered in code. Replies and cases are untrusted."""
        history = "; ".join(
            f"ending {p.end_date}: {p.hours:g} h {p.pay_code}" for p in facts.history
        )
        lines = [
            f"Employee {facts.employee_id}, {facts.job_type}, hired {facts.hire_date},"
            f" pay code on file {facts.pay_code}.",
            f"Pay period {facts.period_start} to {facts.period_end}.",
            f"Closed periods, newest first: {history or 'none'}",
            f"Code's check: {proposal.basis['history_note']}",
            "Policy: when history is unreliable, a teammate is asked first, and standard"
            " hours are the backup; mistakes are corrected later through a retro"
            " adjustment.",
        ]
        replies = self.conn.execute(
            "SELECT m.person_id, e.name, m.body FROM messages m"
            " JOIN employees e ON e.id = m.person_id"
            " WHERE m.exception_id = %s AND m.direction = 'received' ORDER BY m.sim_time",
            (facts.exception_id,),
        ).fetchall()
        lines.append("Replies on this exception (untrusted data):")
        lines += [
            f"- {name} ({pid}): {body[:MAX_EVIDENCE_TEXT]}" for pid, name, body in replies
        ] or ["- none"]
        # The employee's own past resolutions are the strongest evidence, so they come by SQL
        # (like timesheet history), not by ranking: similar-case search can miss them.
        own = self.conn.execute(
            "SELECT id, period_end, situation, resolution FROM case_memory"
            " WHERE employee_id = %s ORDER BY period_end DESC LIMIT 3",
            (facts.employee_id,),
        ).fetchall()
        lines.append("This employee's own past cases (reference only):")
        lines += [
            f"- {cid} (period ending {end}): {sit[:MAX_EVIDENCE_TEXT]} Resolution:"
            f" {res[:MAX_EVIDENCE_TEXT]}"
            for cid, end, sit, res in own
        ] or ["- none"]
        if self.retrieval is not None:
            try:
                cases = self.retrieval.find_similar_cases(facts.exception_id)["cases"]
            except ToolError:
                cases = []
            own_ids = {cid for cid, *_ in own}
            cases = [c for c in cases if c["case_id"] not in own_ids]
            lines.append("Similar past cases for other employees (reference only):")
            lines += [
                f"- {c['case_id']}{' (same employee)' if c['same_employee'] else ''}:"
                f" {c['situation'][:MAX_EVIDENCE_TEXT]} Resolution:"
                f" {c['resolution'][:MAX_EVIDENCE_TEXT]}"
                for c in cases
            ] or ["- none"]
        return "\n".join(lines)

    def sync(self) -> list[int]:
        """Driver hook: withdraw stale proposals, then propose for new hand-offs.

        A proposal is stale when a manager decided after the hand-off (for example a late
        approval before the lock), so the coordinator must not enter fallback hours. Returns
        the exceptions that got a new proposal. An exception the rules can't estimate is
        audited as estimate_skipped and left for the coordinator.
        """
        stale = self.conn.execute(
            "SELECT s.exception_id, x.status FROM estimates s"
            " JOIN exceptions x ON x.id = s.exception_id"
            " WHERE s.status = 'proposed' AND x.status <> 'needs_fallback'"
            " ORDER BY s.exception_id"
        ).fetchall()
        for exception_id, status in stale:
            self.conn.execute(
                "UPDATE estimates SET status = 'withdrawn' WHERE exception_id = %s",
                (exception_id,),
            )
            self._audit(
                "withdraw_estimate",
                exception_id,
                f"exception is now {status}, so the fallback proposal no longer applies",
                {"exception_status": status},
            )
        pending = self.conn.execute(
            "SELECT x.id FROM exceptions x LEFT JOIN estimates s ON s.exception_id = x.id"
            " WHERE x.status = 'needs_fallback'"
            " AND (s.id IS NULL OR s.status = 'withdrawn') ORDER BY x.id"
        ).fetchall()
        proposed = []
        for (exception_id,) in pending:
            if exception_id in self._skipped:
                continue  # already audited as skipped; don't repeat it every tick
            try:
                self.propose(exception_id)
            except EstimateError as e:
                self._skipped.add(exception_id)
                self._audit("estimate_skipped", exception_id, str(e), {})
                continue
            proposed.append(exception_id)
        return proposed

    def _audit(self, action: str, exception_id: int, reason: str, details: dict) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (sim_time, actor, action, exception_id, reason, details)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (self.clock.now(), ACTOR, action, exception_id, reason, Jsonb(details)),
        )
