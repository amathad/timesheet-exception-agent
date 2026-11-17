"""Outbox drafts, merged delivery, batch replies, and the leave-reply escalation rule."""

from datetime import timedelta

import pytest

from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.clock import START, SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator, load_personas
from timesheet_exception_agent.sim.seed import seed
from timesheet_exception_agent.tools.outbox import MergeError, Outbox, template_merge
from timesheet_exception_agent.tools.outreach import OutreachTools, ToolError

E01, E02, E03, E08, E04, E07, E05, E06 = range(1, 9)


@pytest.fixture
def conn():
    with get_conn() as c:
        seed(c)
        yield c
        c.rollback()


@pytest.fixture
def clock():
    return SimClock()


def make(conn, clock, overrides=None):
    sim = ReplySimulator(load_personas(conn), overrides)
    return OutreachTools(conn, clock, sim, outbox=True), Outbox(conn, clock, sim), sim


def count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


# Drafts


def test_send_message_queues_a_draft_and_sets_status(conn, clock):
    tools, _, sim = make(conn, clock)
    result = tools.send_message(E01, "M-01", "teams", "Please submit Avery Brook's time.")
    assert result["status"] == "contacted"
    assert count(conn, "SELECT count(*) FROM outbox WHERE status = 'pending'") == 1
    assert count(conn, "SELECT count(*) FROM messages") == 0
    assert sim.pending() == 0  # nothing reaches the person until delivery
    queued = conn.execute(
        "SELECT details->>'queued' FROM audit_log WHERE action = 'send_message'"
    ).fetchone()[0]
    assert queued == "true"


def test_cap_counts_queued_drafts(conn, clock):
    tools, _, _ = make(conn, clock)
    for _ in range(3):
        tools.send_message(E01, "M-01", "email", "Please submit Avery Brook's time.")
    with pytest.raises(ToolError, match="limit is 3"):
        tools.send_message(E01, "M-01", "email", "Once more.")


# Delivery


def test_single_draft_goes_out_as_written(conn, clock):
    tools, outbox, sim = make(conn, clock)
    tools.send_message(E05, "M-04", "teams", "Please check Emerson Tal's hours.")
    sent = outbox.deliver()
    assert sent == [
        {"person_id": "M-04", "channel": "teams", "exceptions": [E05], "merged_by": None}
    ]
    body, batch = conn.execute("SELECT body, batch_id FROM messages").fetchone()
    assert body == "Please check Emerson Tal's hours." and batch is None
    assert sim.pending() == 1


def test_two_drafts_to_one_person_merge_with_the_router_template(conn, clock):
    tools, outbox, sim = make(conn, clock)
    tools.send_message(E01, "M-01", "teams", "Please submit Avery Brook's time.")
    tools.send_message(E02, "M-01", "teams", "Please approve Blake Corran's time.")
    [group] = outbox.groups()
    body = template_merge(group)
    assert "Avery Brook" in body and "Blake Corran" in body
    [sent] = outbox.deliver()
    assert sent["merged_by"] == "router" and sent["exceptions"] == [E01, E02]
    rows = conn.execute(
        "SELECT exception_id, body, batch_id FROM messages ORDER BY exception_id"
    ).fetchall()
    assert [r[0] for r in rows] == [E01, E02]
    assert rows[0][1] == rows[1][1] == body
    assert rows[0][2] == rows[1][2] is not None
    # The manager answers once; the same reply, naming both, is filed on each exception.
    replies = sim.due(START + timedelta(minutes=10))
    assert [r.exception_id for r in replies] == [E01, E02]
    assert "Avery Brook and Blake Corran" in replies[0].body


def test_different_channels_are_not_merged(conn, clock):
    tools, outbox, _ = make(conn, clock)
    tools.send_message(E01, "M-01", "teams", "Please submit Avery Brook's time.")
    tools.send_message(E02, "M-01", "email", "Please approve Blake Corran's time.")
    assert all(d["merged_by"] is None for d in outbox.deliver())


@pytest.mark.parametrize(
    "body, message",
    [
        ("Please approve Avery Brook's time.", "Blake Corran"),
        ("Avery Brook and Blake Corran, and Dakota Reyes too.", "Dakota Reyes"),
        ("Avery Brook, Blake Corran [escalation > Next level]", "citations"),
        ("Avery Brook, Blake Corran " + "x" * 2000, "longer"),
        ("   ", "empty"),
    ],
)
def test_merge_checks(conn, clock, body, message):
    tools, outbox, _ = make(conn, clock)
    tools.send_message(E01, "M-01", "teams", "Please submit Avery Brook's time.")
    tools.send_message(E02, "M-01", "teams", "Please approve Blake Corran's time.")
    [group] = outbox.groups()
    with pytest.raises(MergeError, match=message):
        outbox.check_merge(group, body)


def test_a_bad_merged_body_falls_back_to_separate_messages(conn, clock):
    tools, outbox, _ = make(conn, clock)
    tools.send_message(E01, "M-01", "teams", "Please submit Avery Brook's time.")
    tools.send_message(E02, "M-01", "teams", "Please approve Blake Corran's time.")
    [group] = outbox.groups()
    sent = outbox.deliver({group.key: ("Only Avery Brook here.", "supervisor")})
    assert [d["merged_by"] for d in sent] == [None, None]
    assert count(conn, "SELECT count(*) FROM audit_log WHERE action = 'merge_refused'") == 1
    assert count(conn, "SELECT count(*) FROM messages WHERE batch_id IS NOT NULL") == 0


def test_a_good_merged_body_is_used(conn, clock):
    tools, outbox, _ = make(conn, clock)
    tools.send_message(E01, "M-01", "teams", "Please submit Avery Brook's time.")
    tools.send_message(E02, "M-01", "teams", "Please approve Blake Corran's time.")
    [group] = outbox.groups()
    body = "Hi Casey, before the 10:00 AM lock: please submit Avery Brook's time and approve Blake Corran's."
    [sent] = outbox.deliver({group.key: (body, "supervisor")})
    assert sent["merged_by"] == "supervisor"
    assert count(conn, "SELECT count(DISTINCT body) FROM messages") == 1


def test_person_on_leave_answers_only_the_first_message(conn, clock):
    tools, outbox, sim = make(conn, clock, {"M-01": "on_leave_teams"})
    tools.send_message(E01, "M-01", "teams", "Please submit Avery Brook's time.")
    outbox.deliver()
    tools.send_message(E02, "M-01", "teams", "Please approve Blake Corran's time.")
    outbox.deliver()
    replies = sim.due(START + timedelta(minutes=10))
    assert [r.exception_id for r in replies] == [E01]
    assert "on leave" in replies[0].body


# Leave replies and early escalation


def reply(conn, exception_id, person_id, body, when=START):
    return conn.execute(
        "INSERT INTO messages (exception_id, person_id, channel, direction, body, sim_time)"
        " VALUES (%s, %s, 'teams', 'received', %s, %s) RETURNING id",
        (exception_id, person_id, body, when),
    ).fetchone()[0]


def test_recorded_leave_reply_allows_early_escalation(conn, clock):
    tools, _, _ = make(conn, clock)
    tools.send_message(E01, "M-01", "teams", "Please submit Avery Brook's time.")
    message_id = reply(conn, E01, "M-01", "I'm on leave today.")
    with pytest.raises(ToolError, match="opens at 09:00"):
        tools.send_message(E01, "N-01", "email", "Casey is out; please submit.")
    tools.record_manager_leave(E01, message_id, "Casey says they are on leave today")
    assert tools.send_message(E01, "N-01", "email", "Casey is out.")["status"] == "escalated"


def test_auto_reply_allows_early_escalation_even_when_presence_is_available(conn, clock):
    tools, _, _ = make(conn, clock)
    tools.send_message(E01, "M-01", "email", "Please submit Avery Brook's time.")
    reply(conn, E01, "M-01", "Automatic reply: I am out of the office.")
    assert tools.send_message(E01, "N-01", "email", "Casey is out.")["status"] == "escalated"


@pytest.mark.parametrize(
    "exception_id, sender, message",
    [(E01, "N-01", "not the manager"), (E02, "M-01", "not a reply received")],
)
def test_record_manager_leave_checks_the_message(conn, clock, exception_id, sender, message):
    tools, _, _ = make(conn, clock)
    message_id = reply(conn, E01, sender, "I'm on leave.")
    with pytest.raises(ToolError, match=message):
        tools.record_manager_leave(exception_id, message_id, "on leave")


def test_record_manager_leave_is_scoped(conn, clock):
    tools, _, _ = make(conn, clock)
    message_id = reply(conn, E01, "M-01", "I'm on leave.")
    with tools.scoped(E02), pytest.raises(ToolError, match="working on exception"):
        tools.record_manager_leave(E01, message_id, "on leave")


def test_reply_to_a_merged_message_lists_the_employees_it_covered(conn, clock):
    tools, outbox, _ = make(conn, clock)
    tools.send_message(E04, "M-03", "teams", "Please submit Dakota Reyes's time.")
    tools.send_message(E07, "M-03", "teams", "Please approve Gray Nakamura's time.")
    outbox.deliver()
    reply(conn, E07, "M-03", "Dakota Reyes and Gray Nakamura's time is approved.",
          START + timedelta(minutes=10))
    result = tools.check_replies(E07)
    [r] = result["replies"]
    assert r["answers_merged_message_about"] == ["Dakota Reyes", "Gray Nakamura"]
    assert "applies to this exception" in result["merged_note"]


def test_reply_to_a_single_message_is_not_marked(conn, clock):
    tools, outbox, _ = make(conn, clock)
    tools.send_message(E05, "M-04", "teams", "Please check Emerson Tal's hours.")
    outbox.deliver()
    reply(conn, E05, "M-04", "Looks right.", START + timedelta(minutes=10))
    result = tools.check_replies(E05)
    assert "answers_merged_message_about" not in result["replies"][0]
    assert "merged_note" not in result


def test_recording_the_same_leave_twice_is_audited_once(conn, clock):
    tools, _, _ = make(conn, clock)
    message_id = reply(conn, E01, "M-01", "I'm on leave today.")
    tools.record_manager_leave(E01, message_id, "on leave")
    assert tools.record_manager_leave(E01, message_id, "on leave")["note"] == "already recorded"
    assert count(
        conn, "SELECT count(*) FROM audit_log WHERE action = 'record_manager_leave'"
    ) == 1
