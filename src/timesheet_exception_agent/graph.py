"""Lock-day workflow as a LangGraph state machine (Module 5).

                       ┌──► outreach ──┐  (one task per wake, in parallel)
    intake ─► plan ────┤               ├──► deliver ─► advance ─┬─► plan   (before 10:00)
                       └───────────────┘  (no wakes)            └─► lock ─┬─► approve ─► report
                                                                          └─► report

The graph (code) is the orchestrator: it owns the clock, timers, deadlines, hand-offs and
when each agent runs.
- intake: Intake loads, dedupes, groups and scores the exceptions (code only).
- plan: the OutreachDriver delivers replies and applies its timers; the rule-based router
  rescores risk, adds manager_out wakes and orders the wakes; on events, the LLM Supervisor
  may adjust the plan through its tools (the router's plan stands if it fails).
- outreach: one task per wake, fanned out with Send and run in parallel (max_concurrency,
  default 4; --sequential sets 1). Each task gets its own database connection,
  OutreachTools and scope, so a reply can still only steer its own exception. send_message
  queues a draft in the outbox.
- deliver: drafts to the same person and channel are merged (the Supervisor's body if it
  wrote one that passes the checks, else the router's template), then delivered; the
  Estimator proposes for any hand-off.
- advance: the shared simulated clock moves 10 minutes.
- lock: the deadline rule hands anything still waiting to the coordinator; the router
  builds the approval queue and the Supervisor may order and annotate it.
- approve: the graph pauses (interrupt) with the queue. The coordinator resumes it with
  decisions, which the approval gate validates and applies in code.
- report: final statuses, read from the database.

Typed state is checkpointed after every step (PostgreSQL in live runs, memory in tests). The
clock, reply simulator and connections are runtime objects, not state, so a run can only be
resumed in the process that started it: enough for the approval pause.

Run a live lock morning (reseeds the database; needs ANTHROPIC_API_KEY unless --baseline;
the A/B script ab.py runs the same morning through run_lock_day):
    uv run python -m timesheet_exception_agent.graph [--baseline] [--supervisor]
        [--sequential | --parallel N] [--duplicate] [--override PERSON=KIND]
        [--no-retrieval] [--decisions FILE]
"""

import operator
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from timesheet_exception_agent.agents.approval import (
    INITIALS,
    ApprovalError,
    ApprovalGate,
    parse_decision,
)
from timesheet_exception_agent.agents.intake import Intake
from timesheet_exception_agent.agents.outreach import OutreachDriver, Wake
from timesheet_exception_agent.agents.router import Router
from timesheet_exception_agent.sim.clock import LOCK, SimClock
from timesheet_exception_agent.sim.personas import ReplySimulator
from timesheet_exception_agent.tools.outbox import Outbox
from timesheet_exception_agent.tools.outreach import DRIVER, OutreachTools

# Supersteps per run: 12 ticks x 4 (plan, outreach, deliver, advance), plus the rest.
RECURSION_LIMIT = 100
DEFAULT_PARALLEL = 4


class WakeTask(TypedDict):
    exception_id: int
    reason: str


class LockDayState(TypedDict, total=False):
    now: str  # simulated time, ISO format
    ticks: int
    intake: dict
    pending: list[WakeTask]  # this tick's wakes, in plan order
    log: Annotated[list[dict], operator.add]  # one entry per Outreach turn
    deliveries: Annotated[list[dict], operator.add]
    handed_off_at_lock: list[int]
    queue: list[dict]
    decisions: list[dict]
    report: dict


@dataclass
class Runtime:
    """Objects the nodes use that are not part of the checkpointed state."""

    conn: Any  # the main connection (plan, deliver, lock, approve)
    connect: Callable[[], Any]  # a new connection for one Outreach task
    clock: SimClock
    sim: ReplySimulator
    make_act: Callable[[OutreachTools], Callable[[Wake], str]]
    driver: OutreachDriver
    intake: Intake
    router: Router
    outbox: Outbox
    gate: ApprovalGate
    supervisor: Any = None
    details: dict = field(default_factory=dict)


def make_runtime(
    conn,
    connect,
    clock,
    sim,
    make_act,
    estimator=None,
    supervisor_factory=None,
    details=None,
) -> Runtime:
    """Wire the runtime. `make_act(tools)` builds the Outreach turn for one task's tools;
    `supervisor_factory(conn, clock, router, outbox)` builds the Supervisor, if any."""
    tools = OutreachTools(conn, clock, sim, outbox=True)
    intake = Intake(conn, clock)
    router = Router(conn, clock, intake)
    outbox = Outbox(conn, clock, sim)

    def no_act(wake: Wake) -> str:  # the graph runs Outreach in its own node
        raise RuntimeError("the graph does not use the driver's act")

    driver = OutreachDriver(conn, clock, tools, no_act, estimator=estimator)
    supervisor = (
        supervisor_factory(conn, clock, router, outbox) if supervisor_factory else None
    )
    return Runtime(
        conn=conn,
        connect=connect,
        clock=clock,
        sim=sim,
        make_act=make_act,
        driver=driver,
        intake=intake,
        router=router,
        outbox=outbox,
        gate=ApprovalGate(conn, clock),
        supervisor=supervisor,
        details=details or {},
    )


def build_graph(rt: Runtime, checkpointer=None):
    def intake(state: LockDayState) -> dict:
        rt.driver.tools.audit(
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

    def plan(state: LockDayState) -> dict:
        wakes = rt.router.plan(rt.driver.due())
        if rt.supervisor is not None:
            wakes = rt.supervisor.review(wakes)
        rt.conn.commit()  # Outreach tasks use their own connections
        return {
            "pending": [{"exception_id": w.exception_id, "reason": w.reason} for w in wakes]
        }

    def fan_out(state: LockDayState):
        tasks = state.get("pending") or []
        return [Send("outreach", t) for t in tasks] if tasks else "deliver"

    def outreach(task: WakeTask) -> dict:
        wake = Wake(task["exception_id"], task["reason"])
        now = rt.clock.now()
        started = time.perf_counter()
        with rt.connect() as conn:
            tools = OutreachTools(conn, rt.clock, rt.sim, outbox=True)
            act = rt.make_act(tools)
            with tools.scoped(wake.exception_id):
                summary = act(wake)
            seconds = round(time.perf_counter() - started, 2)
            # Token use, when the turn used a model (the baseline has none).
            usage = getattr(act, "last_usage", None) or {}
            tools.audit(
                "agent_turn",
                wake.exception_id,
                summary,
                {"wake": wake.reason, "seconds": seconds, **usage},
                actor=DRIVER,
            )
            conn.commit()
        return {
            "log": [
                {
                    "time": f"{now:%H:%M}",
                    "exception_id": wake.exception_id,
                    "wake": wake.reason,
                    "summary": summary,
                    "seconds": seconds,
                }
            ]
        }

    def deliver(state: LockDayState) -> dict:
        merged = {}
        if rt.supervisor is not None:
            merged = rt.supervisor.merge(rt.outbox.groups())
        sent = rt.outbox.deliver(merged)
        if rt.driver.estimator is not None:
            rt.driver.estimator.sync()
        rt.conn.commit()
        now = f"{rt.clock.now():%H:%M}"
        return {
            "ticks": state.get("ticks", 0) + 1,
            "deliveries": [{**d, "time": now} for d in sent],
        }

    def advance(state: LockDayState) -> dict:
        return {"now": rt.clock.advance().isoformat()}

    def after_advance(state: LockDayState) -> str:
        return "lock" if rt.clock.is_past(LOCK) else "plan"

    def lock(state: LockDayState) -> dict:
        handed = rt.driver.lock()
        rt.intake.score()
        queue = rt.router.approval_queue()
        if rt.supervisor is not None:
            queue = rt.supervisor.order_queue(queue)
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
        ("plan", plan),
        ("outreach", outreach),
        ("deliver", deliver),
        ("advance", advance),
        ("lock", lock),
        ("approve", approve),
        ("report", report),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "plan")
    g.add_conditional_edges("plan", fan_out, ["outreach", "deliver"])
    g.add_edge("outreach", "deliver")
    g.add_edge("deliver", "advance")
    g.add_conditional_edges("advance", after_advance, ["plan", "lock"])
    g.add_conditional_edges("lock", after_lock, ["approve", "report"])
    g.add_edge("approve", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def run_config(thread_id: str, parallel: int = DEFAULT_PARALLEL) -> dict:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
        "max_concurrency": parallel,
    }


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
    sent, batches = conn.execute(
        "SELECT count(*), count(DISTINCT coalesce(batch_id::text, 'm' || id))"
        " FROM messages WHERE direction = 'sent'"
    ).fetchone()
    turns, turn_seconds, out_in, out_out = conn.execute(
        "SELECT count(*), coalesce(sum((details->>'seconds')::numeric), 0),"
        " coalesce(sum((details->>'input_tokens')::int), 0),"
        " coalesce(sum((details->>'output_tokens')::int), 0)"
        " FROM audit_log WHERE action = 'agent_turn'"
    ).fetchone()
    calls, tokens_in, tokens_out = conn.execute(
        "SELECT count(*), coalesce(sum((details->>'input_tokens')::int), 0),"
        " coalesce(sum((details->>'output_tokens')::int), 0)"
        " FROM audit_log WHERE action = 'supervisor_call'"
    ).fetchone()
    estimates = [
        {
            "exception_id": x_id,
            "path": path,
            "hours": float(hours),
            "confidence": float(conf),
            "status": status,
            "approved_by": by,
            "final_hours": float(final) if final is not None else None,
            "flag": flag,
        }
        for x_id, path, hours, conf, status, by, final, flag in conn.execute(
            "SELECT exception_id, path, hours, confidence, status, approved_by,"
            " final_hours, flag FROM estimates ORDER BY exception_id"
        )
    ]
    return {
        "exceptions": exceptions,
        "messages_sent": sent,  # one per exception, so a merged message counts per exception
        "deliveries": batches,  # messages people actually received
        "agent_turns": turns,
        "agent_turn_seconds": float(turn_seconds),
        "outreach_tokens": {"input": out_in, "output": out_out},
        "supervisor_calls": calls,
        "supervisor_tokens": {"input": tokens_in, "output": tokens_out},
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
        note = f"\n  Supervisor: {item['supervisor_note']}" if item.get("supervisor_note") else ""
        flag = f"\n  Flag: {item['flag']}" if item.get("flag") else ""
        print(
            f"\n#{item['exception_id']} {item['employee']} ({item['employee_id']}):"
            f" {item['path']}, {item['hours']:g} h {item['pay_code']},"
            f" confidence {item['confidence']}\n  {item['reason']}{flag}{note}"
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


def run_lock_day(
    conn,
    saver,
    *,
    baseline: bool = False,
    supervisor: bool = False,
    duplicate: bool = False,
    overrides: dict | None = None,
    use_retrieval: bool = True,
    parallel: int = DEFAULT_PARALLEL,
    model=None,
    embedder=None,
    decide: Callable[[list[dict]], list[dict]] | None = None,
    on_lock: Callable[[], None] | None = None,
    show: Callable[[dict], None] | None = None,
    details: dict | None = None,
) -> dict:
    """Reseed and run one live lock morning through the graph; return what happened.

    `decide(queue)` answers the approval pause (no answers if None); `on_lock()` runs at the
    pause, before any decision is applied, so callers can read the database as it stood at
    the lock; `show(chunk)` sees every graph update. `model` and `embedder` are shared across
    runs by the caller. Used by the CLI below and by the A/B script (ab.py).
    """
    import uuid

    from langgraph.types import Command

    from timesheet_exception_agent.agents.baseline import FixedReminderBaseline
    from timesheet_exception_agent.agents.estimator import Estimator
    from timesheet_exception_agent.agents.outreach import agent_act, build_agent
    from timesheet_exception_agent.agents.supervisor import Supervisor
    from timesheet_exception_agent.db.connection import get_conn
    from timesheet_exception_agent.sim.personas import load_personas
    from timesheet_exception_agent.sim.seed import add_duplicate, seed

    seed(conn)
    if duplicate:
        add_duplicate(conn)
    conn.commit()
    clock = SimClock()
    sim = ReplySimulator(load_personas(conn), overrides=overrides or {})

    if baseline:
        def make_act(tools):
            return FixedReminderBaseline(tools)
    else:
        retrieval_embedder = embedder if use_retrieval else None

        def make_act(tools):
            retrieval = None
            if retrieval_embedder is not None:
                from timesheet_exception_agent.tools.retrieval import RetrievalTools

                retrieval = RetrievalTools(
                    tools.conn, clock, retrieval_embedder, current_exception=lambda: tools.scope
                )
            agent = build_agent(tools, model=model, retrieval=retrieval)
            return agent_act(agent, clock, retrieval)

    supervisor_factory = None
    if supervisor:
        def supervisor_factory(conn, clock, router, outbox):
            return Supervisor(conn, clock, router, outbox, model=model)

    rt = make_runtime(
        conn, get_conn, clock, sim, make_act, Estimator(conn, clock),
        supervisor_factory, details,
    )
    graph = build_graph(rt, checkpointer=saver)
    thread = run_config(f"lockday-{uuid.uuid4().hex[:8]}", parallel)
    started = time.perf_counter()
    for chunk in graph.stream({}, thread, stream_mode="updates"):
        if "__interrupt__" in chunk:
            break
        if show:
            show(chunk)
    morning_seconds = time.perf_counter() - started
    if on_lock:
        on_lock()
    state = graph.get_state(thread)
    queue = []
    if state.next == ("approve",):
        queue = state.values["queue"]
        answers = decide(queue) if decide else []
        for chunk in graph.stream(Command(resume=answers), thread, stream_mode="updates"):
            if show:
                show(chunk)
        state = graph.get_state(thread)
    return {
        "thread": thread["configurable"]["thread_id"],
        "morning_seconds": morning_seconds,
        "queue": queue,
        "decisions": state.values.get("decisions", []),
        "report": state.values["report"],
    }


if __name__ == "__main__":
    import argparse
    import json
    import os
    import sys

    from dotenv import load_dotenv
    from langgraph.checkpoint.postgres import PostgresSaver

    from timesheet_exception_agent.agents.outreach import (
        default_model,
        model_settings,
        parse_overrides,
    )
    from timesheet_exception_agent.db.connection import get_conn

    parser = argparse.ArgumentParser(description="Run one simulated lock morning as a graph.")
    parser.add_argument("--baseline", action="store_true", help="fixed-reminder baseline, no model")
    parser.add_argument("--supervisor", action="store_true", help="add the LLM Supervisor")
    parser.add_argument("--duplicate", action="store_true", help="add a duplicate exception")
    parser.add_argument("--override", action="append", default=[], metavar="PERSON=KIND")
    parser.add_argument("--no-retrieval", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sequential", action="store_true", help="one Outreach task at a time")
    mode.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, metavar="N")
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
    parallel = 1 if args.sequential else max(1, args.parallel)

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    settings = model_settings()
    needs_model = args.supervisor or not args.baseline
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if needs_model and settings["model"].startswith("anthropic:") and (
        not key or key.startswith("<")
    ):
        raise SystemExit("Set ANTHROPIC_API_KEY in .env, or use --baseline.")
    use_retrieval = not (args.baseline or args.no_retrieval)
    details = {
        "outreach": "fixed_reminder_baseline" if args.baseline else "agent",
        "supervisor": args.supervisor,
        "model": settings["model"] if needs_model else None,
        "effort": settings["effort"] if needs_model else None,
        "parallel": parallel,
        "overrides": overrides,
        "duplicate": args.duplicate,
    }
    print(
        f"Outreach: {details['outreach']}  supervisor: {'on' if args.supervisor else 'off'}"
        f"  model: {details['model'] or '-'}  parallel: {parallel}"
    )
    if overrides:
        print("Overrides: " + ", ".join(f"{p}={k}" for p, k in overrides.items()))

    def show(chunk):
        for node, update in chunk.items():
            if not update:
                continue
            if node == "intake":
                i = update["intake"]
                print(
                    f"Intake: {i['loaded']} loaded, duplicates {i['duplicates'] or 'none'},"
                    f" {i['managers']} managers, risk {i['risk']}"
                )
            elif node == "outreach":
                for e in update["log"]:
                    print(
                        f"{e['time']}  #{e['exception_id']:<2} {e['wake']:<14}"
                        f" ({e['seconds']:.0f}s) {e['summary']}"
                    )
            elif node == "deliver":
                for d in update.get("deliveries", []):
                    if d["merged_by"]:
                        print(
                            f"{d['time']}  merged by {d['merged_by']}: one {d['channel']}"
                            f" to {d['person_id']} for {d['exceptions']}"
                        )
            elif node == "lock":
                print(f"\nAt lock, handed to coordinator: {update['handed_off_at_lock'] or 'none'}")

    def decide(queue):
        print(f"\nApproval queue ({len(queue)} proposals):")
        if args.decisions:
            with open(args.decisions, encoding="utf-8") as f:
                return json.load(f)
        return ask_decisions(queue)

    with get_conn() as conn, PostgresSaver.from_conn_string(
        os.environ["DATABASE_URL"]
    ) as saver:
        saver.setup()
        embedder = None
        if use_retrieval:
            from timesheet_exception_agent.retrieval.embeddings import FastEmbedder
            from timesheet_exception_agent.retrieval.search import (
                CASES,
                POLICIES,
                RetrievalError,
                require_corpus,
            )

            embedder = FastEmbedder()
            try:
                for corpus in (POLICIES, CASES):
                    require_corpus(conn, corpus, embedder.name)
            except RetrievalError as e:
                raise SystemExit(str(e)) from e
            details["retrieval"] = embedder.name

        result = run_lock_day(
            conn,
            saver,
            baseline=args.baseline,
            supervisor=args.supervisor,
            duplicate=args.duplicate,
            overrides=overrides,
            use_retrieval=use_retrieval,
            parallel=parallel,
            model=default_model() if needs_model else None,
            embedder=embedder,
            decide=decide,
            show=show,
            details=details,
        )
        print(f"Thread: {result['thread']}")
        for d in result["decisions"]:
            print(f"  decision: {d}")
        report = result["report"]
        print("\nFinal statuses:")
        for x in report["exceptions"]:
            print(f"  #{x['exception_id']} {x['employee_id']}  {x['status']:<20} risk {x['risk']}")
        print(
            f"Messages: {report['messages_sent']} per exception, {report['deliveries']} delivered."
            f"  Outreach turns: {report['agent_turns']}"
            f" ({report['agent_turn_seconds']:.0f}s of model time,"
            f" tokens in {report['outreach_tokens']['input']},"
            f" out {report['outreach_tokens']['output']})."
            f"  Supervisor calls: {report['supervisor_calls']}"
            f" (tokens in {report['supervisor_tokens']['input']},"
            f" out {report['supervisor_tokens']['output']})."
        )
        print(f"Wall time to the lock: {result['morning_seconds']:.0f}s")
        for e in report["estimates"]:
            print(
                f"  estimate #{e['exception_id']} {e['path']:<14} {e['hours']:g} h"
                f" conf {e['confidence']}  {e['status']}"
                f" by {e['approved_by'] or '-'} final {e['final_hours']}"
                + (f"  flag: {e['flag']}" if e["flag"] else "")
            )
