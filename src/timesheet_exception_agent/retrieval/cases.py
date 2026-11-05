"""Past resolved exceptions from the three closed pay periods in the seed. All synthetic.

They line up with the seed: the same people, the same personas (M-02 and M-05 are often out
of office, M-03 rarely answers, N-02 answers late, M-04 sends corrections) and the hours in
timesheet_history. E-91 and E-92 are earlier new hires who have since left, so they are not
in the employees table.

Loaded into case_memory by retrieval/ingest.py.
"""

from dataclasses import dataclass
from datetime import date

P1, P2, P3 = date(2026, 9, 13), date(2026, 9, 27), date(2026, 10, 11)


@dataclass(frozen=True)
class Case:
    id: str
    period_end: date
    employee_id: str
    manager_id: str | None
    exception_type: str
    situation: str
    resolution: str
    outcome: str
    hours_entered: float | None
    retro: bool = False

    def text(self) -> str:
        """What gets embedded and keyword-indexed."""
        return f"{self.situation} {self.resolution}"


CASES = [
    # Pay period ending 2026-09-13
    Case(
        "P1-01",
        P1,
        "E-01",
        "M-01",
        "not_submitted",
        "Avery Brook forgot to submit time. Manager Casey Thorn showed available on Teams.",
        "Messaged Casey Thorn on Teams at 8:00; they replied at 8:10 and approved 80 hours.",
        "manager_approved",
        80,
    ),
    Case(
        "P1-02",
        P1,
        "E-03",
        "M-02",
        "not_approved",
        "Cameron Ashby's time was unapproved. Manager Devon Marsh was out of office on leave.",
        "Escalated straight to next-level manager Rowan Vale at 8:00, who approved 80 hours"
        " at 8:25.",
        "next_level_approved",
        80,
    ),
    Case(
        "P1-03",
        P1,
        "E-04",
        "M-03",
        "not_submitted",
        "Dakota Reyes had no submitted time. Manager Harper Quill did not answer the first"
        " email.",
        "No reply by the 8:30 follow-up, so escalated to Indira Pell, who replied at 9:20 and"
        " approved 80 hours.",
        "next_level_approved",
        80,
    ),
    Case(
        "P1-04",
        P1,
        "E-10",
        "M-03",
        "unusual_hours",
        "Kai Ferreira showed 72 hours instead of the usual 80. Harper Quill did not reply.",
        "Escalated to Indira Pell, who confirmed at 9:45 that 72 was right: the employee took"
        " an unpaid day off.",
        "next_level_approved",
        72,
    ),
    Case(
        "P1-05",
        P1,
        "E-05",
        "M-04",
        "unusual_hours",
        "Emerson Tal's timesheet showed 84 hours, flagged as unusual.",
        "Manager Jordan Fenwick replied that a missed clock-out added 4 hours and asked for"
        " 80; the coordinator entered the correction before end of day.",
        "correction",
        80,
    ),
    Case(
        "P1-06",
        P1,
        "E-08",
        "M-02",
        "not_submitted",
        "Hollis Varga, part time, had not submitted. Devon Marsh was on leave.",
        "Escalated to Rowan Vale, who approved the usual 48 part-time hours at 8:30.",
        "next_level_approved",
        48,
    ),
    Case(
        "P1-07",
        P1,
        "E-91",
        "M-05",
        "not_submitted",
        "New hire in their first week with no timesheet history. Manager Sasha Lindqvist was"
        " out of office and has no next-level manager.",
        "Asked teammate Jules Okafor, who confirmed the new hire worked 8 hours a day for the"
        " 5 days since starting; the coordinator entered 40 hours.",
        "team_confirmed",
        40,
    ),
    # Pay period ending 2026-09-27
    Case(
        "P2-01",
        P2,
        "E-02",
        "M-01",
        "not_approved",
        "Blake Corran's 76 hours were unapproved, 4 below the usual 80.",
        "Casey Thorn approved 76 as is at 8:15: the employee left early one day and chose not"
        " to use PTO.",
        "manager_approved",
        76,
    ),
    Case(
        "P2-02",
        P2,
        "E-04",
        "M-03",
        "not_approved",
        "Dakota Reyes' time was unapproved. Harper Quill was silent and Indira Pell did not"
        " answer within 30 minutes of escalation.",
        "At the lock the coordinator approved 80 hours from the last two pay periods (80 and"
        " 80, REG), recorded initials and timestamp, and notified Harper Quill, who did not"
        " object.",
        "history_fallback",
        80,
    ),
    Case(
        "P2-03",
        P2,
        "E-07",
        "M-03",
        "not_submitted",
        "Gray Nakamura had not submitted. Harper Quill did not respond to two messages.",
        "Escalated to Indira Pell at 8:30; Indira replied at 9:40 approving 80 hours.",
        "next_level_approved",
        80,
    ),
    Case(
        "P2-04",
        P2,
        "E-09",
        "M-05",
        "not_submitted",
        "Jules Okafor had not submitted. Sasha Lindqvist was out of office and there is no"
        " next-level manager or teammate to ask.",
        "The coordinator approved 80 hours from the last two pay periods with initials and"
        " timestamp and emailed Sasha Lindqvist; no objection came back.",
        "history_fallback",
        80,
    ),
    Case(
        "P2-05",
        P2,
        "E-05",
        "M-04",
        "unusual_hours",
        "Emerson Tal's 80 hours were all coded REG although the employee was out sick one"
        " day.",
        "Jordan Fenwick asked to move 8 hours from REG to the sick pay code; the coordinator"
        " made the change Monday afternoon.",
        "correction",
        80,
    ),
    Case(
        "P2-06",
        P2,
        "E-01",
        "M-01",
        "not_approved",
        "Avery Brook's submitted time was waiting on approval.",
        "Casey Thorn approved 80 hours on Teams within 15 minutes of the first message.",
        "manager_approved",
        80,
    ),
    Case(
        "P2-07",
        P2,
        "E-10",
        "M-03",
        "not_submitted",
        "Kai Ferreira had not submitted. Harper Quill was silent again.",
        "Escalated to Indira Pell after the 30-minute follow-up; Indira approved 80 hours at"
        " 9:30.",
        "next_level_approved",
        80,
    ),
    Case(
        "P2-08",
        P2,
        "E-92",
        "M-05",
        "not_submitted",
        "New hire with no history and an unknown schedule. Sasha Lindqvist was out of office,"
        " there is no next-level manager, and teammate Jules Okafor did not know the hours.",
        "By end of day Monday the coordinator entered standard hours for the job type, 8 hours"
        " a day for 10 days (80). The employee had worked 76, so 4 hours were taken back as a"
        " retro adjustment on the next paycheck.",
        "standard_hours",
        80,
        True,
    ),
    # Pay period ending 2026-10-11
    Case(
        "P3-01",
        P3,
        "E-07",
        "M-03",
        "unusual_hours",
        "Gray Nakamura showed 64 hours after two periods of 80. Harper Quill did not reply.",
        "Escalated to Indira Pell, who confirmed at 9:45 that the employee moved to a reduced"
        " schedule this period; 64 hours approved. Earlier 80-hour periods no longer predict"
        " this employee's hours.",
        "next_level_approved",
        64,
    ),
    Case(
        "P3-02",
        P3,
        "E-03",
        "M-02",
        "not_approved",
        "Cameron Ashby's time was unapproved. Devon Marsh's Teams status showed out of"
        " office.",
        "Escalated immediately to Rowan Vale, who approved 80 hours at 8:20.",
        "next_level_approved",
        80,
    ),
    Case(
        "P3-03",
        P3,
        "E-04",
        "M-03",
        "not_submitted",
        "Dakota Reyes had not submitted. Harper Quill and Indira Pell both stayed silent past"
        " the lock.",
        "The coordinator approved 80 hours from history with initials and timestamp. On"
        " Wednesday Harper Quill said the employee worked 82, so 2 hours went on the next"
        " paycheck as a retro adjustment.",
        "history_fallback",
        80,
        True,
    ),
    Case(
        "P3-04",
        P3,
        "E-08",
        "M-02",
        "not_submitted",
        "Hollis Varga, part time, had not submitted. Devon Marsh was on leave.",
        "Escalated to Rowan Vale, who approved 48 hours at 8:35.",
        "next_level_approved",
        48,
    ),
    Case(
        "P3-05",
        P3,
        "E-02",
        "M-01",
        "not_submitted",
        "Blake Corran had not submitted time before the report ran.",
        "Casey Thorn had the employee submit and approved 80 hours by 8:20.",
        "manager_approved",
        80,
    ),
    Case(
        "P3-06",
        P3,
        "E-05",
        "M-04",
        "not_approved",
        "Emerson Tal's time was unapproved. Jordan Fenwick replied that they were in meetings"
        " and would approve later.",
        "Jordan Fenwick approved 80 hours at 8:50 after one reminder, before the 9:00"
        " cut-off, so no escalation was needed.",
        "manager_approved",
        80,
    ),
]
