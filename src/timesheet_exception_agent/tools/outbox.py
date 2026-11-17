"""Outbox delivery (Module 5): merge drafts to the same person, then deliver them.

In graph runs, Outreach's send_message queues a draft in `outbox` (still one exception per
wake, scoped as before). At the end of the tick the graph:
1. groups the pending drafts by recipient and channel;
2. merges each group of two or more: with the Supervisor's body if it wrote one that passes
   the checks, otherwise with the router's template;
3. delivers: one `messages` row per exception, sharing a batch_id, and one simulated send,
   so the person answers the merged message once and the reply is filed on each exception.

Merge checks, in code (`check_merge`): one recipient and channel, every employee in the
group named, no employee with an exception outside the group named, no policy citations,
within the length limit. A group whose merge fails the checks is delivered as separate
messages, so nothing is ever lost.

Status and follow-up timers were already set when the draft was queued; delivery only sends.
The caller owns the transaction.
"""

from dataclasses import dataclass, field

from psycopg.types.json import Jsonb

from timesheet_exception_agent.retrieval.search import CITATION
from timesheet_exception_agent.sim.clock import SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator
from timesheet_exception_agent.tools.outreach import DRIVER, MAX_BODY


class MergeError(Exception):
    """A merged body that fails the checks. The message is safe to show to the model."""


@dataclass(frozen=True)
class Draft:
    id: int
    exception_id: int
    person_id: str
    role: str
    channel: str
    body: str
    employee: str  # the exception's employee name


@dataclass
class Group:
    person_id: str
    person: str  # recipient name
    channel: str
    drafts: list[Draft] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.person_id}/{self.channel}"


def template_merge(group: Group) -> str:
    """The router's merge: a fixed header, then each draft as written, numbered."""
    parts = [
        (
            f"Hi {group.person}, {len(group.drafts)} timesheets need your action before"
            " the 10:00 AM lock today:"
        )
    ]
    for n, d in enumerate(group.drafts, 1):
        parts.append(f"{n}) {d.employee}: {d.body}")
    return "\n\n".join(parts)


class Outbox:
    def __init__(self, conn, clock: SimClock, sim: ReplySimulator) -> None:
        self.conn = conn
        self.clock = clock
        self.sim = sim

    def groups(self) -> list[Group]:
        """Pending drafts by recipient and channel, oldest draft first."""
        rows = self.conn.execute(
            "SELECT o.id, o.exception_id, o.person_id, p.name, o.role, o.channel, o.body,"
            "  e.name FROM outbox o JOIN employees p ON p.id = o.person_id"
            " JOIN exceptions x ON x.id = o.exception_id"
            " JOIN employees e ON e.id = x.employee_id"
            " WHERE o.status = 'pending' ORDER BY o.exception_id, o.id"
        ).fetchall()
        groups: dict[tuple[str, str], Group] = {}
        for d_id, x_id, person_id, person, role, channel, body, employee in rows:
            g = groups.setdefault(
                (person_id, channel), Group(person_id, person, channel)
            )
            g.drafts.append(Draft(d_id, x_id, person_id, role, channel, body, employee))
        return list(groups.values())

    def check_merge(self, group: Group, body: str) -> str:
        """Return the body if it may go out as one message for the group, else raise."""
        body = body.strip()
        if len(group.drafts) < 2:
            raise MergeError("only groups of two or more drafts are merged")
        if not body:
            raise MergeError("the merged body is empty")
        if len(body) > MAX_BODY:
            raise MergeError(f"the merged body is longer than {MAX_BODY} characters")
        if CITATION.search(body):
            raise MergeError("policy citations stay out of messages to people")
        missing = [d.employee for d in group.drafts if d.employee not in body]
        if missing:
            raise MergeError(f"the merged body must name {', '.join(missing)}")
        ids = [d.exception_id for d in group.drafts]
        others = [
            name
            for (name,) in self.conn.execute(
                "SELECT DISTINCT e.name FROM exceptions x"
                " JOIN employees e ON e.id = x.employee_id"
                " WHERE x.id <> ALL(%s) AND x.status <> 'duplicate'",
                (ids,),
            )
            if name not in {d.employee for d in group.drafts}
        ]
        named = [n for n in others if n in body]
        if named:
            raise MergeError(
                f"the merged body names {', '.join(named)}, whose exceptions are not in"
                " this group"
            )
        return body

    def deliver(self, merged: dict[str, tuple[str, str]] | None = None) -> list[dict]:
        """Deliver every pending draft.

        `merged` maps a group key to (body, merged_by) for groups to send as one message;
        bodies are checked again here. Other groups of two or more get the router's
        template; groups of one are sent as drafted. Returns what was sent.
        """
        merged = merged or {}
        sent = []
        for group in self.groups():
            body, by = None, None
            if len(group.drafts) > 1:
                body, by = merged.get(group.key, (template_merge(group), "router"))
                try:
                    body = self.check_merge(group, body)
                except MergeError as e:
                    self._audit(
                        "merge_refused",
                        group.drafts[0].exception_id,
                        str(e),
                        {"group": group.key, "merged_by": by,
                         "drafts": [d.id for d in group.drafts]},
                    )
                    body, by = None, None
            if body is None:
                for d in group.drafts:
                    sent.append(self._send([d], d.body, None))
            else:
                sent.append(self._send(group.drafts, body, by))
        return sent

    def _send(self, drafts: list[Draft], body: str, merged_by: str | None) -> dict:
        now = self.clock.now()
        batch_id = drafts[0].id if merged_by else None
        first = drafts[0]
        for d in drafts:
            self.conn.execute(
                "INSERT INTO messages (exception_id, person_id, channel, direction, body,"
                " sim_time, batch_id) VALUES (%s, %s, %s, 'sent', %s, %s, %s)",
                (d.exception_id, d.person_id, d.channel, body, now, batch_id),
            )
            self.conn.execute(
                "UPDATE outbox SET status = 'delivered', batch_id = %s WHERE id = %s",
                (batch_id, d.id),
            )
            self._audit(
                "deliver_message",
                d.exception_id,
                f"delivered to {d.role}" + (f", merged by {merged_by}" if merged_by else ""),
                {
                    "person_id": d.person_id,
                    "channel": d.channel,
                    "draft_id": d.id,
                    "batch_id": batch_id,
                    "merged_by": merged_by,
                    "exceptions": [x.exception_id for x in drafts],
                },
            )
        self.sim.on_sent_batch(
            [(d.exception_id, d.employee) for d in drafts], first.person_id, first.channel, now
        )
        return {
            "person_id": first.person_id,
            "channel": first.channel,
            "exceptions": [d.exception_id for d in drafts],
            "merged_by": merged_by,
        }

    def _audit(self, action, exception_id, reason, details) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (sim_time, actor, action, exception_id, reason, details)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (self.clock.now(), DRIVER, action, exception_id, reason, Jsonb(details)),
        )
