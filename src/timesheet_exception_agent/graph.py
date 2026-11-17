"""Lock-day workflow as a LangGraph state machine (Module 5).

    start ─► intake ─► tick ─► advance ─┬─► tick            (before 10:00 AM)
                                        └─► lock ─┬─► approve ─► report   (proposals waiting)
                                                  └─► report             (nothing to approve)

- intake: Intake loads, dedupes, groups and scores the exceptions (code only).
- tick: the OutreachDriver delivers replies and applies its timers; the rule-based router
  rescores risk, adds manager_out wakes and orders the wakes by risk; then Outreach (the
  ReAct agent, or the fixed-reminder baseline) works each wake, and the Estimator proposes
  for any hand-off. Wakes in one tick run one after another, in router order: the tools
  share one database connection, the scope guard and the reply simulator.
- advance: the shared simulated clock moves 10 minutes.
- lock: the deadline rule hands anything still waiting to the coordinator; the router
  builds the approval queue.
- approve: the graph pauses (interrupt) with the queue. The coordinator resumes it with
  decisions, which the approval gate validates and applies in code.
- report: final statuses, read from the database.

Typed state is checkpointed after every step (PostgreSQL in live runs, memory in tests), so
the approval pause can be resumed later. The clock, reply simulator and database connection
are runtime objects, not state, so a run can only be resumed in the process that started
it: enough for the approval pause, not for restarting a morning halfway.

Run a live lock morning (reseeds the database; needs ANTHROPIC_API_KEY unless --baseline):
    uv run python -m timesheet_exception_agent.graph [--baseline] [--duplicate]
        [--override PERSON=KIND] [--no-retrieval] [--decisions FILE]
"""

import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from timesheet_exception_agent.agents.approval import (
    INITIALS,
    ApprovalError,
    ApprovalGate,
    parse_decision,
)
from timesheet_exception_agent.agents.intake import Intake
from timesheet_exception_agent.agents.outreach import OutreachDriver
from timesheet_exception_agent.agents.router import Router
from timesheet_exception_agent.sim.clock import LOCK, SimClock
from timesheet_exception_agent.tools.outreach import DRIVER, OutreachTools

# Steps per run: 12 ticks x 2 nodes, plus intake, lock, approve and report.
RECURSION_LIMIT = 100


class LockDayState(TypedDict, total=False):
    now: str  # simulated time, ISO format
    ticks: int
    intake: dict
    log: Annotated[list[dict], operator.add]  # one entry per agent turn
    handed_off_at_lock: list[int]
    queue: list[dict]
    decisions: list[dict]
    report: dict


@dataclass
class Runtime:
    """Objects the nodes use that are not part of the checkpointed state."""

    conn: Any
    clock: SimClock
    tools: OutreachTools
    driver: OutreachDriver
    intake: Intake
    router: Router
    gate: ApprovalGate
    details: dict


def make_runtime(conn, clock, tools, act, estimator=None, details=None) -> Runtime:
    intake = Intake(conn, clock)
    router = Router(conn, clock, intake)
    driver = OutreachDriver(
        conn, clock, tools, act, estimator=estimator, plan=router.plan
    )
    return Runtime(
        conn=conn,
        clock=clock,
        tools=tools,
        driver=driver,
        intake=intake,
        router=router,
        gate=ApprovalGate(conn, clock),
        details=details or {},
    )


def build_graph(rt: Runtime, checkpointer=None):
    def intake(state: LockDayState) -> dict:
        rt.tools.audit(
            "run_started",
            None,
            f"lock-day graph run until {LOCK:%H:%M %Z}",
            {**rt.details, "graph": True},
            actor=DRIVER,
        )
        summary = rt.intake.run()
        rt.conn.commit()
        return {
            "now": rt.clock.now().isoformat(),
            "ticks": 0,
            "intake": {
                "loaded": summary["loaded"],
                "duplicates": {str(k): v for k, v in summary["duplicates"].items()},
                "managers": summary["managers"],
                "risk": {str(k): v for k, v in summary["risk"].items()},
            },
        }

    def tick(state: LockDayState) -> dict:
        now = rt.clock.now()
        done = rt.driver.tick()
        return {
            "ticks": state.get("ticks", 0) + 1,
            "log": [
                {
                    "time": f"{now:%H:%M}",
                    "exception_id": wake.exception_id,
                    "wake": wake.reason,
                    "summary": summary,
                }
                for wake, summary in done
            ],
        }

    def advance(state: LockDayState) -> dict:
        return {"now": rt.clock.advance().isoformat()}

    def after_advance(state: LockDayState) -> str:
        return "lock" if rt.clock.is_past(LOCK) else "tick"

    def lock(state: LockDayState) -> dict:
        handed = rt.driver.lock()
        rt.intake.score()
        queue = rt.router.approval_queue()
        rt.conn.commit()
        return {"handed_off_at_lock": handed, "queue": queue}

    def after_lock(state: LockDayState) -> str:
        return "approve" if state.get("queue") else "report"

    def approve(state: LockDayState) -> dict:
        # Everything before interrupt() reruns on resume, so nothing is written before it.
        answers = interrupt({"queue": state["queue"]})
        results = []
        for item in answers or []:
            try:
                results.append(rt.gate.apply(parse_decision(item)))
            except (ApprovalError, KeyError, TypeError, ValueError) as e:
                results.append({"exception_id": item.get("exception_id"), "error": str(e)})
        rt.intake.score()  # closed exceptions drop to zero risk
        rt.conn.commit()
        return {"decisions": results}

    def report(state: LockDayState) -> dict:
        return {"report": final_report(rt.conn)}

    g = StateGraph(LockDayState)
    for name, fn in [
        ("intake", intake),
        ("tick", tick),
        ("advance", advance),
        ("lock", lock),
        ("approve", approve),
        ("report", report),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "tick")
    g.add_edge("tick", "advance")
    g.add_conditional_edges("advance", after_advance, ["tick", "lock"])
    g.add_conditional_edges("lock", after_lock, ["approve", "report"])
    g.add_edge("approve", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def final_report(conn) -> dict:
    """Final state of the run, read from the database."""
    exceptions = [
        {
            "exception_id": x_id,
            "employee_id": emp,
            "status": status,
            "risk": float(risk) if risk is not None else None,
        }
        for x_id, emp, status, risk in conn.execute(
            "SELECT x.id, x.employee_id, x.status, t.risk FROM exceptions x"
            " LEFT JOIN triage t ON t.exception_id = x.id ORDER BY x.id"
        )
    ]
    sent = conn.execute(
        "SELECT count(*) FROM messages WHERE direction = 'sent'"
    ).fetchone()[0]
    turns = conn.execute(
        "SELECT count(*) FROM audit_log WHERE action = 'agent_turn'"
    ).fetchone()[0]
    estimates = [
        {
            "exception_id": x_id,
            "path": path,
            "hours": float(hours),
            "confidence": float(conf),
            "status": status,
            "approved_by": by,
            "final_hours": float(final) if final is not None else None,
        }
        for x_id, path, hours, conf, status, by, final in conn.execute(
            "SELECT exception_id, path, hours, confidence, status, approved_by,"
            " final_hours FROM estimates ORDER BY exception_id"
        )
    ]
    return {
        "exceptions": exceptions,
        "messages_sent": sent,
        "agent_turns": turns,
        "estimates": estimates,
    }


def ask_decisions(queue: list[dict]) -> list[dict]:
    """Collect the coordinator's decisions on the console (stdin can be piped)."""
    # PowerShell pipes text with a byte-order mark in front; drop it.
    initials = input("Coordinator initials: ").lstrip("﻿").strip().upper()
    if not INITIALS.match(initials):
        raise SystemExit("initials must be 2 or 3 letters")
    out = []
    for item in queue:
        print(
            f"\n#{item['exception_id']} {item['employee']} ({item['employee_id']}):"
            f" {item['path']}, {item['hours']:g} h {item['pay_code']},"
            f" confidence {item['confidence']}\n  {item['reason']}"
        )
        answer = input("  [a]pprove, [e]dit HOURS [CODE], [r]eject: ").split()
        kind = {"a": "approve", "e": "edit", "r": "reject"}.get(
            answer[0].lower() if answer else "", "reject"
        )
        decision = {"exception_id": item["exception_id"], "decision": kind, "initials": initials}
        if kind == "edit":
            decision["hours"] = float(answer[1]) if len(answer) > 1 else None
            if len(answer) > 2:
                decision["pay_code"] = answer[2].upper()
        out.append(decision)
    return out


if __name__ == "__main__":
    import argparse
    import json
    import os
    import sys
    import uuid

    from dotenv import load_dotenv
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.types import Command

    from timesheet_exception_agent.agents.baseline import FixedReminderBaseline
    from timesheet_exception_agent.agents.estimator import Estimator
    from timesheet_exception_agent.agents.outreach import (
        agent_act,
        build_agent,
        model_settings,
        parse_overrides,
    )
    from timesheet_exception_agent.db.connection import get_conn
    from timesheet_exception_agent.sim.personas import ReplySimulator, load_personas
    from timesheet_exception_agent.sim.seed import add_duplicate, seed

    parser = argparse.ArgumentParser(description="Run one simulated lock morning as a graph.")
    parser.add_argument("--baseline", action="store_true", help="fixed-reminder baseline, no model")
    parser.add_argument("--duplicate", action="store_true", help="add a duplicate exception")
    parser.add_argument("--override", action="append", default=[], metavar="PERSON=KIND")
    parser.add_argument("--no-retrieval", action="store_true")
    parser.add_argument(
        "--decisions",
        metavar="FILE",
        help="JSON list of coordinator decisions instead of asking on the console",
    )
    args = parser.parse_args()
    try:
        overrides = parse_overrides(args.override)
    except ValueError as e:
        parser.error(str(e))

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    settings = model_settings()
    use_retrieval = not (args.baseline or args.no_retrieval)
    details = {
        "outreach": "fixed_reminder_baseline" if args.baseline else "agent",
        "model": None if args.baseline else settings["model"],
        "effort": None if args.baseline else settings["effort"],
        "overrides": overrides,
        "duplicate": args.duplicate,
    }
    print(f"Outreach: {details['outreach']}  model: {details['model'] or '-'}")
    if overrides:
        print("Overrides: " + ", ".join(f"{p}={k}" for p, k in overrides.items()))

    with get_conn() as conn, PostgresSaver.from_conn_string(
        os.environ["DATABASE_URL"]
    ) as saver:
        saver.setup()
        seed(conn)
        if args.duplicate:
            add_duplicate(conn)
        conn.commit()
        clock = SimClock()
        sim = ReplySimulator(load_personas(conn), overrides=overrides)
        tools = OutreachTools(conn, clock, sim)
        if args.baseline:
            act = FixedReminderBaseline(tools)
        else:
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if settings["model"].startswith("anthropic:") and (
                not key or key.startswith("<")
            ):
                raise SystemExit("Set ANTHROPIC_API_KEY in .env, or use --baseline.")
            retrieval = None
            if use_retrieval:
                from timesheet_exception_agent.retrieval.embeddings import FastEmbedder
                from timesheet_exception_agent.retrieval.search import (
                    CASES,
                    POLICIES,
                    RetrievalError,
                    require_corpus,
                )
                from timesheet_exception_agent.tools.retrieval import RetrievalTools

                embedder = FastEmbedder()
                try:
                    for corpus in (POLICIES, CASES):
                        require_corpus(conn, corpus, embedder.name)
                except RetrievalError as e:
                    raise SystemExit(str(e)) from e
                retrieval = RetrievalTools(
                    conn, clock, embedder, current_exception=lambda: tools.scope
                )
                details["retrieval"] = embedder.name
            act = agent_act(build_agent(tools, retrieval=retrieval), clock, retrieval)
        rt = make_runtime(conn, clock, tools, act, Estimator(conn, clock), details)
        graph = build_graph(rt, checkpointer=saver)
        thread = {
            "configurable": {"thread_id": f"lockday-{uuid.uuid4().hex[:8]}"},
            "recursion_limit": RECURSION_LIMIT,
        }
        print(f"Thread: {thread['configurable']['thread_id']}\n")

        def show(chunk):
            for node, update in chunk.items():
                if node == "intake":
                    i = update["intake"]
                    print(
                        f"Intake: {i['loaded']} loaded, duplicates {i['duplicates'] or 'none'},"
                        f" {i['managers']} managers, risk {i['risk']}"
                    )
                elif node == "tick":
                    for e in update["log"]:
                        print(
                            f"{e['time']}  #{e['exception_id']:<2} {e['wake']:<14} {e['summary']}"
                        )
                elif node == "lock":
                    print(f"\nAt lock, handed to coordinator: {update['handed_off_at_lock'] or 'none'}")

        for chunk in graph.stream({}, thread, stream_mode="updates"):
            if "__interrupt__" in chunk:
                break
            show(chunk)
        state = graph.get_state(thread)
        if state.next == ("approve",):
            queue = state.values["queue"]
            print(f"\nApproval queue ({len(queue)} proposals, lowest confidence first):")
            if args.decisions:
                with open(args.decisions, encoding="utf-8") as f:
                    answers = json.load(f)
            else:
                answers = ask_decisions(queue)
            for chunk in graph.stream(Command(resume=answers), thread, stream_mode="updates"):
                show(chunk)
            state = graph.get_state(thread)
            for d in state.values.get("decisions", []):
                print(f"  decision: {d}")
        report = state.values["report"]
        print("\nFinal statuses:")
        for x in report["exceptions"]:
            print(f"  #{x['exception_id']} {x['employee_id']}  {x['status']:<20} risk {x['risk']}")
        print(f"Messages sent: {report['messages_sent']}  agent turns: {report['agent_turns']}")
        for e in report["estimates"]:
            print(
                f"  estimate #{e['exception_id']} {e['path']:<14} {e['hours']:g} h"
                f" conf {e['confidence']}  {e['status']}"
                f" by {e['approved_by'] or '-'} final {e['final_hours']}"
            )
