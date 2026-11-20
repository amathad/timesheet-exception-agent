# Timesheet Exception Agent

A multi-agent system that helps a payroll coordinator clear timesheet exceptions before the payroll lock. It contacts managers, reads their replies, escalates when they are out or silent, and proposes fallback hours from pay history for the coordinator to approve.

Capstone project for the Carnegie Mellon University School of Computer Science program *Agent AI Development for Real World Applications* (2026), Forecaster option.

> **All data is synthetic.** The workflow was described by a practicing payroll coordinator; no names, reports or records from any employer are used.

## The problem

Before each payroll, an exception report lists every employee whose time is unsubmitted or unapproved. A coordinator covering about 3 states works 20–30 of these per payroll, under a hard deadline: timesheets lock at **10:00 AM Monday**. Anything left open becomes a retro adjustment on a later paycheck.

The agent works the lock morning (8:00 → 10:00 AM) on a simulated clock:

1. Contact each employee's manager by email or Teams.
2. If the manager is on leave, escalate to the next-level manager right away. A manager who is replying but hasn't decided is escalated at 9:00 AM. The next level gets 30 minutes.
3. If no one decides, propose hours from the employee's last two pay periods, or, when history is unreliable, ask a teammate and fall back to standard hours for the job.
4. The coordinator approves, edits or rejects every proposal, with initials. **The agent never approves time itself.**

## How it works

```
intake ─► tick ─► plan (router) ─► Outreach × N (parallel) ─► deliver (merge, Estimator) ─► advance clock
            ▲                                                                                   │
            └──────────────────────────────── until 10:00 lock ─────────────────────────────────┘
                                                   │
                                                   ▼
                                lock ─► coordinator approval (interrupt) ─► report
```

| Component | Role | Model? |
| --- | --- | --- |
| **Intake** (`agents/intake.py`) | Loads the period, marks duplicates, groups by manager, scores lock risk | Code only |
| **Router** (`agents/router.py`) | Rescores risk each tick, orders wakes, builds the approval queue | Code only |
| **Outreach** (`agents/outreach.py`) | ReAct agent per exception: writes messages, interprets replies, chooses the next step | Claude |
| **Estimator** (`agents/estimator.py`, `agents/tot.py`) | Hours from the last two closed periods; gated Tree-of-Thought when history conflicts | Code, Claude for ToT |
| **Supervisor** (`agents/supervisor.py`) | Optional LLM coordinator across exceptions (`--supervisor`) | Claude |
| **Approval gate** (`agents/approval.py`) | Coordinator approve / edit / reject with initials and time | Human |
| **Baseline** (`agents/baseline.py`) | Fixed reminders, never escalates; the comparison arm | Code only |

The workflow is a LangGraph `StateGraph` (`graph.py`) checkpointed to PostgreSQL, with an `interrupt` at the approval pause.

**Design principles**
- **Business rules live in code**: deadlines, timers, the escalation chain, the 3-message cap, hour calculations. The model writes messages, interprets replies, explains estimates and chooses the next action, and every tool refuses illegal moves.
- **Manager replies are untrusted.** A deterministic reply screen (`tools/guardrails.py`) flags instructions, bulk requests and secrecy; a flagged reply can't be recorded as a decision.
- **Grounded retrieval.** pgvector + PostgreSQL full-text search fused by reciprocal rank, over synthetic policy documents and past cases, always filtered on the employee's state and the run's date. Citations appear only in status reasons, never in messages to managers, and must have been retrieved for that exception.
- **Everything is audited** in `audit_log`: every tool call, retrieval (query, filters, sources), refusal and model turn.

## Results

From 63 A/B runs and 32 evaluation runs on Claude Haiku 5.5 (details in [`docs/AB_RESULTS.md`](docs/AB_RESULTS.md) and [`docs/EVAL_RESULTS.md`](docs/EVAL_RESULTS.md)):

| | Agent | Fixed-reminder baseline |
| --- | --- | --- |
| Manager decisions before the lock (9 scenarios) | 57 | 25 |
| Scenarios with a wrong outcome | 0 of 9 | 9 of 9 |
| Wrongful approvals | 0 | 11 |
| Messages sent | 100 | 172 |

- Reply-classification accuracy 140/140; 65/65 citations grounded; 0 injection successes.
- Cost about **$0.04 per simulated morning**, about 55 s wall time with 4 parallel Outreach tasks.
- The LLM Supervisor changed no outcome against the rule-based router, at 17% more tokens and twice the wall time, so the **router is the default**.
- Known limitation: fallback confidence values are placeholders (Brier 0.373; standard-hours confidence is too low on this seed).

## Setup

Requirements: Windows/macOS/Linux, [uv](https://docs.astral.sh/uv/), Docker, and a Claude API key from the [Claude Console](https://console.anthropic.com) (not covered by a Claude Pro subscription). The `--baseline` run needs no key.

```powershell
git clone <this repo>
cd timesheet-exception-agent
copy .env.example .env      # then set POSTGRES_PASSWORD, DATABASE_URL and ANTHROPIC_API_KEY
docker compose up -d        # PostgreSQL 16 + pgvector, container payroll-pg
uv sync
```

Load the schema and synthetic data, then the retrieval corpus (embeddings run locally with fastembed; the first run downloads the model):

```powershell
uv run python -m timesheet_exception_agent.sim.seed
```

```powershell
uv run python -m timesheet_exception_agent.retrieval.ingest
```

Check the connection:

```powershell
uv run python -m timesheet_exception_agent.db.connection
```

## Running

One simulated lock morning (reseeds the database; press Enter at the approval prompt to see the queue and decide):

```powershell
uv run python -m timesheet_exception_agent.graph
```

Useful options:

| Option | Effect |
| --- | --- |
| `--baseline` | Fixed-reminder baseline, no model |
| `--supervisor` | Add the LLM Supervisor |
| `--override PERSON=KIND` | Change a person's reply, e.g. `N-02=silent`, `M-01=injection`, `M-03=use_history` |
| `--duplicate` | Add a duplicate exception |
| `--no-retrieval`, `--no-tot` | Turn off retrieval or Tree-of-Thought |
| `--sequential`, `--parallel N` | Outreach concurrency (default 4) |
| `--decisions FILE` | Coordinator decisions from a JSON file instead of the console |
| `--off GUARDRAIL` | Switch a guardrail off (ablation only) |

Check results in the database, not in the printed summary:

```powershell
docker exec payroll-pg psql -U postgres -d payroll -c "SELECT id, employee_id, type, status FROM exceptions ORDER BY id;"
```

### Evaluation

```powershell
uv run python -m timesheet_exception_agent.metrics --summary
```

```powershell
uv run python -m timesheet_exception_agent.ab --summary
```

```powershell
uv run python -m timesheet_exception_agent.retrieval.eval
```

Without `--summary`, `metrics` and `ab` run new live scenarios (they reseed and call the model). `metrics --ablation` turns each guardrail off in turn.

### MCP server

A read-only [MCP](https://modelcontextprotocol.io) server exposes the latest run to any MCP client (Claude Desktop, Claude Code, the MCP Inspector). It has 7 tools and none that send, change a status or approve. Setup for each client is in [`docs/MCP_SERVER.md`](docs/MCP_SERVER.md).

```powershell
uv run python -m timesheet_exception_agent.mcp_server
```

### Tracing (optional)

Set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` in `.env` to trace graph runs in LangSmith.

## Tests

```powershell
uv run pytest
```

330 tests, no API calls (scripted fake models, hash embeddings). They reseed the `payroll` database and roll back retrieval changes, so don't run them during a live run.

## Repository layout

```
src/timesheet_exception_agent/
├── agents/       outreach, estimator, tot, intake, router, supervisor, approval, baseline
├── tools/        Outreach and retrieval tools, outbox (merged delivery), reply screen and guardrails
├── retrieval/    policies/*.md, cases.py, embeddings, ingest, hybrid search, gold-set eval
├── sim/          simulated clock, manager personas, synthetic seed
├── db/           schema.sql, connection
├── graph.py      LangGraph lock-day workflow
├── ab.py         Supervisor vs router vs baseline A/B
├── metrics.py    evaluation over the audit log, ablation
└── mcp_server.py read-only MCP server
tests/            pytest suite
docs/             project context, live runs, evaluation results
```

## Documentation

| File | Contents |
| --- | --- |
| [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) | Problem, workflow rules, design decisions, build status, session log |
| [`docs/LIVE_RUNS.md`](docs/LIVE_RUNS.md) | Every live run with findings and fixes |
| [`docs/AB_RESULTS.md`](docs/AB_RESULTS.md) | Supervisor vs router vs baseline |
| [`docs/EVAL_RESULTS.md`](docs/EVAL_RESULTS.md) | Module 6 metrics, reply screen, ablation |
| [`docs/RETRIEVAL_EVAL.md`](docs/RETRIEVAL_EVAL.md) | Retrieval gold-set evaluation |
| [`docs/MCP_SERVER.md`](docs/MCP_SERVER.md) | MCP server tools and client setup |

## Stack

Python 3.12 · uv · LangGraph / LangChain · Claude (Haiku 5.5 for runs; `claude-opus-5-5` code default) via `langchain-anthropic` · PostgreSQL 16 + pgvector in Docker · fastembed (`BAAI/bge-small-en-v1.5`) · official `mcp` SDK · LangSmith · pytest, ruff
