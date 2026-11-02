from datetime import timedelta

import pytest

from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.clock import LOCK, START, SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator, load_personas
from timesheet_exception_agent.sim.seed import seed
from timesheet_exception_agent.tools.outreach import OutreachTools, ToolError

# Exception ids follow the seed's EXCEPTIONS order.
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


@pytest.fixture
def tools(conn, clock):
    return OutreachTools(conn, clock, ReplySimulator(load_personas(conn)))


def audit_actions(conn, exception_id):
    rows = conn.execute(
        "SELECT action FROM audit_log WHERE exception_id = %s ORDER BY id",
        (exception_id,),
    )
    return [r[0] for r in rows]


# Read tools


def test_get_time_before_and_after_lock(tools, clock):
    assert tools.get_time() == {
        "now": START.isoformat(),
        "minutes_to_lock": 120,
        "locked": False,
        "phase": "before_manager_window",
    }
    clock.advance_to(LOCK)
    assert tools.get_time()["phase"] == "locked_coordinator_only"
    assert tools.get_time()["locked"] is True


def test_get_exception_includes_manager_and_teammates(tools):
    info = tools.get_exception(E06)
    assert info["employee"]["id"] == "E-06"
    assert info["manager"]["id"] == "M-05"
    assert info["teammates"] == [{"id": "E-09", "name": "Jules Okafor"}]
    assert info["status"] == "open"


def test_get_exception_unknown_id(tools):
    with pytest.raises(ToolError):
        tools.get_exception(999)


def test_presence_shows_manager_on_leave(tools):
    assert tools.get_presence("M-02")["status"] == "out_of_office"
    assert tools.get_presence("M-01")["status"] == "available"
    assert tools.get_presence("E-01")["status"] == "unknown"


def test_next_level_manager(tools):
    assert tools.get_next_level_manager("M-02")["next_level_manager"]["id"] == "N-01"
    assert tools.get_next_level_manager("M-05")["next_level_manager"] is None


# send_message and check_replies


def test_contacting_manager_sets_status_timer_and_audit(tools, conn):
    result = tools.send_message(E01, "M-01", "teams", "Please approve Avery's time.")
    assert result["role"] == "manager"
    assert result["status"] == "contacted"
    assert result["next_check_at"] == (START + timedelta(minutes=30)).isoformat()
    assert audit_actions(conn, E01) == ["send_message"]


def test_responsive_reply_arrives_after_ten_minutes(tools, clock, conn):
    tools.send_message(E01, "M-01", "teams", "Please approve Avery's time.")
    assert tools.check_replies(E01)["replies"] == []
    clock.advance()
    replies = tools.check_replies(E01)["replies"]
    assert len(replies) == 1
    assert replies[0]["from_id"] == "M-01"
    assert "approved" in replies[0]["body"]
    assert audit_actions(conn, E01) == ["send_message", "reply_received"]


def test_replies_are_delivered_once_and_flagged_untrusted(tools, clock, conn):
    tools.send_message(E01, "M-01", "teams", "Please approve.")
    clock.advance()
    tools.check_replies(E01)
    result = tools.check_replies(E01)
    assert len(result["replies"]) == 1
    assert "untrusted" in result["note"]
    received = conn.execute(
        "SELECT count(*) FROM messages WHERE direction = 'received'"
    )
    assert received.fetchone()[0] == 1


def test_escalating_to_next_level_sets_escalated(tools):
    tools.send_message(E03, "M-02", "email", "Please approve Cameron's time.")
    result = tools.send_message(
        E03, "N-01", "teams", "Cameron's manager is out. Can you approve?"
    )
    assert result["role"] == "next_level_manager"
    assert result["status"] == "escalated"


def test_out_of_office_reply_on_email(tools):
    tools.send_message(E03, "M-02", "email", "Please approve Cameron's time.")
    body = tools.check_replies(E03)["replies"][0]["body"]
    assert body.startswith("Automatic reply")


def test_teammate_message_keeps_status(tools):
    result = tools.send_message(
        E06, "E-09", "teams", "Do you know Finley's hours last week?"
    )
    assert result["role"] == "teammate"
    assert result["status"] == "open"


def test_cannot_message_people_outside_the_chain(tools, conn):
    with pytest.raises(ToolError):
        tools.send_message(E01, "M-03", "email", "Hello")
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize(
    "channel, body", [("sms", "Hi"), ("teams", "   "), ("email", "x" * 2001)]
)
def test_send_message_validates_channel_and_body(tools, channel, body):
    with pytest.raises(ToolError):
        tools.send_message(E01, "M-01", channel, body)


def test_follow_up_is_capped_at_lock(tools, clock):
    clock.advance_to(LOCK - timedelta(minutes=10))
    result = tools.send_message(E01, "M-01", "teams", "Please approve.")
    assert result["next_check_at"] == LOCK.isoformat()


# update_status guardrails


def test_record_approval(tools, conn):
    tools.send_message(E01, "M-01", "teams", "Please approve.")
    result = tools.update_status(
        E01, "resolved", "Manager replied that the time is approved."
    )
    assert result == {
        "exception_id": E01,
        "status": "resolved",
        "previous_status": "contacted",
    }
    assert audit_actions(conn, E01) == ["send_message", "update_status"]


@pytest.mark.parametrize("status", ["closed", "contacted", "escalated", "open"])
def test_agent_cannot_set_coordinator_or_code_owned_statuses(tools, status):
    with pytest.raises(ToolError):
        tools.update_status(E01, status, "trying anyway")


def test_reason_is_required(tools):
    tools.send_message(E01, "M-01", "teams", "Please approve.")
    with pytest.raises(ToolError):
        tools.update_status(E01, "resolved", " ")


def test_cannot_resolve_without_contacting_anyone(tools):
    with pytest.raises(ToolError):
        tools.update_status(E01, "resolved", "assumed fine")


def test_cannot_resolve_after_lock(tools, clock):
    tools.send_message(E04, "N-02", "teams", "Please approve Dakota's time.")
    clock.advance_to(LOCK + timedelta(minutes=20))
    tools.check_replies(E04)
    with pytest.raises(ToolError, match="locked"):
        tools.update_status(E04, "resolved", "Next-level manager approved late.")
    assert tools.update_status(E04, "needs_fallback", "Approval came after lock.")[
        "status"
    ] == ("needs_fallback")


def test_no_messages_after_resolved(tools):
    tools.send_message(E01, "M-01", "teams", "Please approve.")
    tools.update_status(E01, "resolved", "Approved by manager.")
    with pytest.raises(ToolError):
        tools.send_message(E01, "M-01", "teams", "Reminder")


# LangChain wrappers


def test_as_tools_exposes_seven_tools_and_returns_errors(tools):
    by_name = {t.name: t for t in tools.as_tools()}
    assert set(by_name) == {
        "get_time",
        "get_exception",
        "get_presence",
        "get_next_level_manager",
        "check_replies",
        "send_message",
        "update_status",
    }
    assert set(by_name["send_message"].args) == {
        "exception_id",
        "person_id",
        "channel",
        "body",
    }
    result = by_name["update_status"].invoke(
        {"exception_id": E01, "status": "closed", "reason": "done"}
    )
    assert "error" in result
