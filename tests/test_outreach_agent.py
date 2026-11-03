from datetime import timedelta

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from timesheet_exception_agent.agents.outreach import (
    OutreachDriver,
    Wake,
    agent_act,
    build_agent,
    default_model,
    model_settings,
)
from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.clock import LOCK, START, SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator, load_personas
from timesheet_exception_agent.sim.seed import seed
from timesheet_exception_agent.tools.outreach import OutreachTools

# Exception ids follow the seed's EXCEPTIONS order.
E01, E02, E03, E08, E04, E07, E05, E06 = range(1, 9)
ALL = list(range(1, 9))


@pytest.fixture
def conn():
    with get_conn() as c:
        seed(c)
        c.commit()  # the driver commits, so start from a committed seed
        yield c
        c.rollback()


@pytest.fixture
def clock():
    return SimClock()


@pytest.fixture
def tools(conn, clock):
    return OutreachTools(conn, clock, ReplySimulator(load_personas(conn)))


def statuses(conn):
    return dict(
        conn.execute("SELECT id, status FROM exceptions ORDER BY id").fetchall()
    )


class Recorder:
    """Stand-in for the model: emails the manager on first contact and records approvals."""

    def __init__(self, tools):
        self.tools = tools
        self.wakes = []

    def __call__(self, wake: Wake) -> str:
        self.wakes.append((self.tools.clock.now(), wake))
        if wake.reason == "first_contact":
            manager = self.tools.get_exception(wake.exception_id)["manager"]
            self.tools.send_message(
                wake.exception_id, manager["id"], "email", "Please approve."
            )
            return "emailed manager"
        if wake.reason == "new_reply":
            last = self.tools.check_replies(wake.exception_id)["replies"][-1]["body"]
            if "approved" in last:
                self.tools.update_status(
                    wake.exception_id, "resolved", "manager approved"
                )
                return "resolved"
        return "waiting"


# Driver


def test_first_tick_wakes_every_exception_for_first_contact(conn, clock, tools):
    driver = OutreachDriver(conn, clock, tools, Recorder(tools))
    assert driver.due() == [Wake(i, "first_contact") for i in ALL]


def test_replies_wake_only_their_exceptions(conn, clock, tools):
    act = Recorder(tools)
    driver = OutreachDriver(conn, clock, tools, act)
    driver.tick()  # 8:00: email every manager
    clock.advance()
    # 8:10: responsive replies for E-01/E-02; out-of-office auto-replies arrived at 8:00.
    woken = {w.exception_id: w.reason for w in driver.due()}
    assert woken == {
        E01: "new_reply",
        E02: "new_reply",
        E03: "new_reply",
        E08: "new_reply",
        E06: "new_reply",
    }


def test_follow_up_timer_wakes_silent_manager_after_thirty_minutes(conn, clock, tools):
    driver = OutreachDriver(conn, clock, tools, Recorder(tools))
    driver.tick()
    clock.advance(timedelta(minutes=20))
    driver.tick()  # handle replies, resolve E-01/E-02
    clock.advance(timedelta(minutes=10))
    due = {w.exception_id: w.reason for w in driver.due()}
    assert due[E04] == "follow_up_due"  # silent M-03
    assert E01 not in due and E02 not in due


def test_run_resolves_responsive_and_hands_off_the_rest_at_lock(conn, clock, tools):
    act = Recorder(tools)
    handed_off = OutreachDriver(conn, clock, tools, act).run()
    assert clock.now() == LOCK
    final = statuses(conn)
    assert final[E01] == final[E02] == "resolved"
    assert handed_off == [i for i in ALL if i not in (E01, E02)]
    assert all(final[i] == "needs_fallback" for i in handed_off)
    actors = conn.execute(
        "SELECT DISTINCT actor FROM audit_log WHERE action = 'update_status'"
        " AND exception_id = %s",
        (E04,),
    ).fetchall()
    assert actors == [("outreach_driver",)]


def test_driver_commits_and_audits_each_agent_turn(conn, clock, tools):
    OutreachDriver(conn, clock, tools, Recorder(tools)).tick()
    with get_conn() as other:  # a separate connection only sees committed rows
        turns = other.execute(
            "SELECT count(*) FROM audit_log WHERE action = 'agent_turn'"
        ).fetchone()[0]
    assert turns == len(ALL)


def test_needs_fallback_wakes_on_late_reply_but_not_on_timer(conn, clock, tools):
    tools.send_message(
        E04, "N-02", "teams", "Please approve Dakota's time."
    )  # slow, 75 min
    tools.update_status(E04, "needs_fallback", "no reply in 30 minutes")
    driver = OutreachDriver(conn, clock, tools, Recorder(tools))
    clock.advance(timedelta(minutes=40))
    assert E04 not in {w.exception_id for w in driver.due()}
    clock.advance_to(START + timedelta(minutes=75))
    assert Wake(E04, "new_reply") in driver.due()


# Agent wiring (scripted model, no API calls)


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args, id):
    return {"name": name, "args": args, "id": id, "type": "tool_call"}


def test_agent_uses_tools_and_returns_summary(conn, clock, tools):
    script = iter(
        [
            AIMessage(
                "", tool_calls=[call("get_exception", {"exception_id": E01}, "1")]
            ),
            AIMessage(
                "",
                tool_calls=[
                    call(
                        "send_message",
                        {
                            "exception_id": E01,
                            "person_id": "M-01",
                            "channel": "teams",
                            "body": "Hi Casey, please approve Avery Brook's time before the 10:00 AM lock.",
                        },
                        "2",
                    )
                ],
            ),
            AIMessage("Messaged Casey Thorn on Teams to approve Avery Brook's time."),
        ]
    )
    agent = build_agent(tools, ScriptedModel(messages=script))
    summary = agent_act(agent, clock)(Wake(E01, "first_contact"))
    assert summary.startswith("Messaged Casey Thorn")
    assert statuses(conn)[E01] == "contacted"


def test_agent_sees_rule_errors_and_continues(conn, clock, tools):
    script = iter(
        [
            AIMessage(
                "",
                tool_calls=[
                    call(
                        "update_status",
                        {
                            "exception_id": E01,
                            "status": "closed",
                            "reason": "done",
                        },
                        "1",
                    )
                ],
            ),
            AIMessage("Could not close the exception; only the coordinator can."),
        ]
    )
    agent = build_agent(tools, ScriptedModel(messages=script))
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "Close exception 1."}]}
    )
    tool_reply = next(m for m in result["messages"] if m.type == "tool")
    assert "error" in tool_reply.content
    assert statuses(conn)[E01] == "open"


# Claude model settings (payload only, no API calls)


def claude_payload(monkeypatch, name):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    model = default_model(name)
    return model._get_request_payload([{"role": "user", "content": "hi"}])


def test_default_model_is_claude_with_effort_and_no_temperature(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_EFFORT", raising=False)
    payload = claude_payload(monkeypatch, None)
    assert payload["model"] == "claude-opus-5-5"
    assert payload["output_config"] == {"effort": "medium"}
    assert payload["max_tokens"] == 16000
    assert "temperature" not in payload


def test_fallbacks_only_for_models_that_support_them(monkeypatch):
    opus = claude_payload(monkeypatch, "anthropic:claude-opus-5-5")
    assert opus["fallbacks"] == "default"
    assert opus["betas"] == ["server-side-fallback-2026-07-01"]
    haiku = claude_payload(monkeypatch, "anthropic:claude-haiku-5-5")
    assert "fallbacks" not in haiku and "betas" not in haiku


def test_llm_model_and_effort_come_from_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "anthropic:claude-sonnet-5-5")
    monkeypatch.setenv("LLM_EFFORT", "low")
    payload = claude_payload(monkeypatch, None)
    assert payload["model"] == "claude-sonnet-5-5"
    assert payload["output_config"] == {"effort": "low"}


# Run metadata


def test_run_records_model_settings_in_audit_log(conn, clock, tools):
    details = {"model": "anthropic:claude-haiku-5-5", "effort": "medium"}
    OutreachDriver(conn, clock, tools, Recorder(tools)).run(details=details)
    rows = conn.execute(
        "SELECT actor, exception_id, details, sim_time FROM audit_log"
        " WHERE action = 'run_started'"
    ).fetchall()
    assert rows == [("outreach_driver", None, details, START)]


def test_model_settings_follow_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "anthropic:claude-haiku-5-5")
    monkeypatch.setenv("LLM_EFFORT", "low")
    assert model_settings() == {"model": "anthropic:claude-haiku-5-5", "effort": "low"}
    monkeypatch.delenv("LLM_MODEL")
    monkeypatch.delenv("LLM_EFFORT")
    assert model_settings() == {
        "model": "anthropic:claude-opus-5-5",
        "effort": "medium",
    }


def test_model_settings_has_no_effort_for_other_providers(monkeypatch):
    assert model_settings("ollama:qwen2.5:7b") == {
        "model": "ollama:qwen2.5:7b",
        "effort": None,
    }


# Next-level timeout (code, not prompt)


def test_escalation_times_out_to_coordinator_without_waking_agent(conn, clock, tools):
    tools.send_message(
        E04, "N-02", "teams", "Please approve Dakota's time."
    )  # slow, 75 min
    act = Recorder(tools)
    driver = OutreachDriver(conn, clock, tools, act)
    clock.advance(timedelta(minutes=30))
    assert E04 not in {w.exception_id for w in driver.due()}
    assert statuses(conn)[E04] == "needs_fallback"
    actor, reason = conn.execute(
        "SELECT actor, reason FROM audit_log WHERE action = 'update_status'"
        " AND exception_id = %s",
        (E04,),
    ).fetchone()
    assert actor == "outreach_driver" and "30 minutes" in reason


def test_reply_arriving_at_the_timeout_wins(conn, clock, tools):
    tools.send_message(E03, "N-01", "teams", "Please approve Cameron's time.")  # 10 min
    tools.conn.execute(
        "UPDATE exceptions SET next_check_at = %s WHERE id = %s",
        (START + timedelta(minutes=10), E03),
    )
    clock.advance()
    driver = OutreachDriver(conn, clock, tools, Recorder(tools))
    assert Wake(E03, "new_reply") in driver.due()
    assert statuses(conn)[E03] == "escalated"


def test_manager_follow_up_still_wakes_agent(conn, clock, tools):
    tools.send_message(E04, "M-03", "teams", "Please submit Dakota's time.")  # silent
    clock.advance(timedelta(minutes=30))
    driver = OutreachDriver(conn, clock, tools, Recorder(tools))
    assert Wake(E04, "follow_up_due") in driver.due()
    assert statuses(conn)[E04] == "contacted"
