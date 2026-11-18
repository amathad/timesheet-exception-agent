"""The A/B script: scenarios, outcome checks and metrics, run with the baseline arm (no model)
and an in-memory checkpointer. No API calls."""

from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from timesheet_exception_agent.ab import (
    ARMS,
    DEFAULT,
    GENUINE_APPROVAL,
    SCENARIOS,
    run_one,
    summarize,
)
from timesheet_exception_agent.agents.outreach import token_usage
from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.sim.clock import START
from timesheet_exception_agent.sim.personas import (
    REPLIES,
    STALLING_NOTES,
    ReplySimulator,
)
from timesheet_exception_agent.sim.seed import seed

SETTINGS = {"model": None, "effort": None}


@pytest.fixture
def conn():
    with get_conn() as c:
        seed(c)
        c.commit()  # the graph commits
        yield c
        c.rollback()


def body(kind: str, employee: str = "Avery Brook", turn: int = 0) -> str:
    template = REPLIES[kind][1]
    if isinstance(template, tuple):
        template = template[turn]
    return template.format(employee=employee)


def test_every_scenario_expects_a_status_for_every_seeded_exception():
    assert len(SCENARIOS) == 9
    for s in SCENARIOS.values():
        expected = s.expected()
        assert set(DEFAULT) <= set(expected)
        assert ("dup" in expected) == s.duplicate
        assert all(kind in REPLIES for kind in s.overrides.values())


@pytest.mark.parametrize("kind", ["responsive", "slow", "late", "injection"])
def test_genuine_approvals_match(kind):
    assert GENUINE_APPROVAL.search(body(kind))


@pytest.mark.parametrize(
    "kind", ["ambiguous", "use_history", "correction", "on_leave_teams", "on_leave"]
)
def test_replies_that_approve_nothing_do_not_match(kind):
    assert not GENUINE_APPROVAL.search(body(kind))


@pytest.mark.parametrize("note", STALLING_NOTES)
def test_holding_notes_do_not_match(note):
    # "I'll approve ... right after my 9:30" is a promise, not an approval.
    assert not GENUINE_APPROVAL.search(note.format(employee="Avery Brook"))


def test_late_manager_approves_95_minutes_after_the_first_message():
    sim = ReplySimulator({"M-03": "silent"}, {"M-03": "late"})
    replies = sim.on_sent_batch(
        [(5, "Dakota Reyes"), (6, "Gray Nakamura")], "M-03", "email", START
    )
    assert {r.exception_id for r in replies} == {5, 6}
    assert all(r.sim_time == START + timedelta(minutes=95) for r in replies)
    assert "Dakota Reyes and Gray Nakamura's time is approved" in replies[0].body
    # A reminder gets no second reply.
    assert sim.on_sent(5, "M-03", "email", "Dakota Reyes", START + timedelta(minutes=30)) is None


def test_token_usage_sums_model_messages():
    messages = [
        HumanMessage("task"),
        AIMessage("a", usage_metadata={"input_tokens": 100, "output_tokens": 10,
                                       "total_tokens": 110}),
        AIMessage("b", usage_metadata={"input_tokens": 50, "output_tokens": 5,
                                       "total_tokens": 55}),
    ]
    assert token_usage(messages) == {"input_tokens": 150, "output_tokens": 15}
    assert token_usage([HumanMessage("x")]) == {"input_tokens": 0, "output_tokens": 0}


def test_baseline_default_morning_metrics_are_read_at_the_lock(conn):
    row = run_one(conn, InMemorySaver(), SCENARIOS["default"], "baseline", 1, None, None,
                  SETTINGS)
    # The baseline never escalates and takes E-05's correction as an approval.
    assert row["escalated"] == 0
    assert row["status"]["E-05"] == "resolved"
    assert "E-05" in row["wrong"] and "E-05" in row["wrongful_approvals"]
    assert row["status"]["E-01"] == "resolved" and "E-01" not in row["wrong"]
    assert row["decided"] == sum(s in ("resolved", "correction_requested")
                                 for s in row["status"].values())
    assert row["to_coordinator"] == sum(s == "needs_fallback" for s in row["status"].values())
    assert row["to_coordinator"] == row["queue"]
    assert row["outreach_tokens"] == {"input": 0, "output": 0}
    assert row["supervisor_calls"] == 0
    # Metrics are as of the lock; the run then finished with everything approved.
    assert all(row["final_status"][k] == "closed"
               for k, s in row["status"].items() if s == "needs_fallback")
    assert conn.execute(
        "SELECT DISTINCT approved_by FROM estimates WHERE status = 'approved'"
    ).fetchall() == [("TC",)]


def test_duplicate_scenario_keeps_the_duplicate_out_of_outreach(conn):
    row = run_one(conn, InMemorySaver(), SCENARIOS["duplicate"], "baseline", 1, None, None,
                  SETTINGS)
    assert row["status"]["dup"] == "duplicate"
    assert "dup" not in row["wrong"]
    assert row["duplicate_messaged"] == 0


def test_summary_has_a_row_per_scenario_and_arm_and_lists_failures():
    row = {
        "scenario": "default", "arm": "router", "rep": 1, "decided": 7, "wrong": [],
        "wrongful_approvals": [], "to_coordinator": 1, "messages": 10, "deliveries": 6,
        "escalated": 4, "outreach_tokens": {"input": 40000, "output": 4000},
        "supervisor_tokens": {"input": 0, "output": 0}, "supervisor_calls": 0,
        "wall_seconds": 36.0,
    }
    text = summarize([row, {**row, "rep": 2, "decided": 6, "wrong": ["E-07"]},
                      {"scenario": "late", "arm": "supervisor", "rep": 1, "error": "boom"}])
    assert "| default | router | 2 | 6.5 (6–7) | 0.5 (0–1) (E-07) |" in text
    assert "| router | 2 | 6.5 | 1 of 2 |" in text
    assert "late/supervisor/1: boom" in text
    assert set(ARMS) == {"router", "supervisor", "baseline"}
