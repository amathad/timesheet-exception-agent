"""The lock-day graph, run with the fixed-reminder baseline (no model) and an in-memory
checkpointer."""

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from tests.test_outreach_agent import ScriptedModel
from timesheet_exception_agent.agents.approval import (
    ApprovalError,
    ApprovalGate,
    Decision,
)
from timesheet_exception_agent.agents.baseline import FixedReminderBaseline
from timesheet_exception_agent.agents.estimator import Estimator
from timesheet_exception_agent.agents.supervisor import Supervisor
from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.graph import (
    build_graph,
    final_report,
    make_runtime,
    run_config,
)
from timesheet_exception_agent.sim.clock import LOCK, SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator, load_personas
from timesheet_exception_agent.sim.seed import add_duplicate, seed
from timesheet_exception_agent.tools.outreach import OutreachTools, ToolError

E01, E02, E03, E08, E04, E07, E05, E06 = range(1, 9)
CONFIG = run_config("t1")


@pytest.fixture
def conn():
    with get_conn() as c:
        seed(c)
        c.commit()  # the graph commits
        yield c
        c.rollback()


def make(conn, overrides=None, duplicate=False, make_act=FixedReminderBaseline,
         supervisor_factory=None):
    if duplicate:
        add_duplicate(conn)
        conn.commit()
    clock = SimClock()
    sim = ReplySimulator(load_personas(conn), overrides)
    rt = make_runtime(
        conn, get_conn, clock, sim, make_act, Estimator(conn, clock),
        supervisor_factory, {"outreach": "fixed_reminder_baseline"},
    )
    return rt, build_graph(rt, checkpointer=InMemorySaver())


def statuses(conn):
    return dict(conn.execute("SELECT id, status FROM exceptions ORDER BY id").fetchall())


def test_graph_pauses_for_approval_at_the_lock(conn):
    rt, graph = make(conn)
    graph.invoke({}, CONFIG)
    state = graph.get_state(CONFIG)
    assert state.next == ("approve",)
    assert rt.clock.is_past(LOCK)
    assert state.values["ticks"] == 12  # 08:00 to 09:50
    assert state.values["handed_off_at_lock"] == [E03, E08, E04, E07, E06]
    queue = [q["exception_id"] for q in state.values["queue"]]
    assert set(queue) == {E03, E08, E04, E07, E06}
    # Nothing is approved before the coordinator answers.
    assert conn.execute(
        "SELECT count(*) FROM estimates WHERE status <> 'proposed' OR approved_by IS NOT NULL"
    ).fetchone()[0] == 0


def test_resume_applies_the_coordinators_decisions(conn):
    _, graph = make(conn)
    graph.invoke({}, CONFIG)
    result = graph.invoke(
        Command(
            resume=[
                {"exception_id": E04, "decision": "approve", "initials": "tc"},
                {"exception_id": E07, "decision": "edit", "hours": 64, "initials": "TC"},
                {"exception_id": E06, "decision": "reject", "initials": "TC"},
                {"exception_id": E01, "decision": "approve", "initials": "TC"},  # no proposal
            ]
        ),
        CONFIG,
    )
    assert graph.get_state(CONFIG).next == ()
    errors = [d for d in result["decisions"] if "error" in d]
    assert [d["exception_id"] for d in errors] == [E01]
    final = statuses(conn)
    assert final[E04] == final[E07] == "closed"
    assert final[E06] == "needs_fallback"
    rows = {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT exception_id, status, approved_by, hours, final_hours FROM estimates"
        )
    }
    assert rows[E04][:2] == ("approved", "TC") and float(rows[E04][3]) == 80
    assert rows[E07][:2] == ("edited", "TC")
    assert float(rows[E07][2]) == 80 and float(rows[E07][3]) == 64  # proposal kept
    assert rows[E06][:2] == ("rejected", "TC") and rows[E06][3] is None
    audit = conn.execute(
        "SELECT action, exception_id, details->>'initials' FROM audit_log"
        " WHERE actor = 'coordinator' ORDER BY id"
    ).fetchall()
    assert audit == [
        ("approve_estimate", E04, "TC"),
        ("edit_estimate", E07, "TC"),
        ("reject_estimate", E06, "TC"),
    ]
    report = result["report"]
    assert {x["exception_id"]: x["status"] for x in report["exceptions"]} == final
    risk = {x["exception_id"]: x["risk"] for x in report["exceptions"]}
    assert risk[E04] == risk[E07] == 0.0 and risk[E06] == 1.0


def test_duplicate_is_never_worked(conn):
    _, graph = make(conn, duplicate=True)
    state = graph.invoke({}, CONFIG)
    assert state["intake"]["duplicates"] == {"9": E01}
    assert statuses(conn)[9] == "duplicate"
    assert conn.execute(
        "SELECT count(*) FROM messages WHERE exception_id = 9"
    ).fetchone()[0] == 0
    assert all(e["exception_id"] != 9 for e in state["log"])


def test_no_queue_goes_straight_to_the_report(conn):
    everyone = {m: "responsive" for m in ("M-02", "M-03", "M-05")}
    _, graph = make(conn, overrides=everyone)
    state = graph.invoke({}, CONFIG)
    assert graph.get_state(CONFIG).next == ()
    assert state["queue"] == [] and "decisions" not in state
    assert "report" in state


def test_run_is_audited_as_a_graph_run(conn):
    _, graph = make(conn)
    graph.invoke({}, CONFIG)
    details = conn.execute(
        "SELECT details FROM audit_log WHERE action = 'run_started'"
    ).fetchone()[0]
    assert details == {"outreach": "fixed_reminder_baseline", "graph": True}


# Approval gate on its own (rolled back, no graph)


@pytest.fixture
def gate_setup(conn):
    clock = SimClock()
    tools = OutreachTools(conn, clock, ReplySimulator(load_personas(conn)))
    tools.hand_off([E04], "test")
    Estimator(conn, clock).sync()
    return ApprovalGate(conn, clock)


@pytest.mark.parametrize(
    "decision, message",
    [
        (Decision(E04, "approve", "T"), "initials"),
        (Decision(E04, "approve", "T1"), "initials"),
        (Decision(E04, "sign", "TC"), "decision"),
        (Decision(E04, "edit", "TC"), "hours"),
        (Decision(E04, "edit", "TC", hours=500), "hours"),
        (Decision(E01, "approve", "TC"), "no proposal"),
    ],
)
def test_gate_refuses_bad_decisions(gate_setup, decision, message):
    with pytest.raises(ApprovalError, match=message):
        gate_setup.apply(decision)


def test_gate_refuses_a_second_decision(gate_setup):
    gate_setup.apply(Decision(E04, "approve", "TC"))
    with pytest.raises(ApprovalError, match="not waiting"):
        gate_setup.apply(Decision(E04, "reject", "TC"))


# Parallel fan-out


def outcome(conn):
    return (
        statuses(conn),
        conn.execute(
            "SELECT exception_id, person_id, count(*) FROM messages WHERE direction = 'sent'"
            " GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall(),
    )


def test_parallel_and_sequential_runs_give_the_same_outcome(conn):
    _, graph = make(conn)
    graph.invoke({}, run_config("par", parallel=4))
    parallel = outcome(conn)
    seed(conn)
    conn.commit()
    _, graph = make(conn)
    graph.invoke({}, run_config("seq", parallel=1))
    assert outcome(conn) == parallel


def test_each_task_has_its_own_connection_and_scope(conn):
    seen = []

    class Probe(FixedReminderBaseline):
        def __call__(self, wake):
            seen.append((id(self.tools.conn), self.tools.scope, wake.exception_id))
            other = E02 if wake.exception_id != E02 else E01
            with pytest.raises(ToolError, match="working on exception"):
                self.tools.update_status(other, "needs_fallback", "steered by a reply")
            return super().__call__(wake)

    rt, graph = make(conn, make_act=Probe)
    graph.invoke({}, CONFIG)
    assert seen and all(scope == x for _, scope, x in seen)
    assert id(rt.conn) not in {c for c, _, _ in seen}


def test_router_merges_drafts_to_the_same_manager(conn):
    _, graph = make(conn)
    state = graph.invoke({}, CONFIG)
    merged = [d for d in state["deliveries"] if d["merged_by"]]
    first = {(d["person_id"], tuple(d["exceptions"])) for d in merged if d["time"] == "08:00"}
    # The baseline emails every manager at 08:00: M-01, M-02 and M-03 have two each.
    assert first == {("M-01", (E01, E02)), ("M-02", (E03, E08)), ("M-03", (E04, E07))}
    report = final_report(conn)  # the graph is paused for approval
    assert report["deliveries"] < report["messages_sent"]


def test_supervisor_that_changes_nothing_leaves_the_router_outcome(conn):
    _, graph = make(conn)
    graph.invoke({}, run_config("router"))
    router_outcome = outcome(conn)
    seed(conn)
    conn.commit()

    def quiet(conn, clock, router, outbox):
        replies = (AIMessage("Nothing to change.") for _ in range(1000))
        return Supervisor(conn, clock, router, outbox, model=ScriptedModel(messages=replies))

    _, graph = make(conn, supervisor_factory=quiet)
    graph.invoke({}, run_config("sup"))  # pauses for approval
    assert outcome(conn) == router_outcome
    assert final_report(conn)["supervisor_calls"] > 0
