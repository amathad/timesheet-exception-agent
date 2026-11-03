# Project Context: Timesheet Exception Agent

Capstone project for the CMU School of Computer Science program *Agent AI Development for Real World Applications* (Sept–Nov 2026). This file records the problem, the rules the agent must follow, the design decisions made so far, and where the build stands. Update it at the end of each working session.

**Capstone option:** Forecaster
**Status as of 2026-11-02:** database seeded; simulated clock, persona reply generator, Outreach tools, and Outreach agent with driver loop built (tested with a scripted model; no live LLM run yet).

---

## 1. Problem

Before each payroll, an exception report lists every employee whose time is unsubmitted or unapproved, with their manager. In the reference organization (8,000+ employees across 9–10 states), each payroll coordinator covers about 3 states and 20–30 exceptions per payroll, spending about half a day per pay period resolving them under hard deadlines.

The workflow was described by a practicing payroll coordinator. **All project data is synthetic.** No real names, reports, or screenshots from any employer.

**Intended user:** the payroll coordinator. The agent communicates with managers, next-level managers, and teammates.

## 2. Workflow rules (from domain interviews)

### Timeline

| When | What happens |
| --- | --- |
| Before Monday | Exception report is ready |
| Monday 8:00 AM | Coordinators start working exceptions |
| Monday 9:00–10:00 AM | Managers' final correction window |
| **Monday 10:00 AM** | **Timesheets lock**; managers can no longer change them |
| Monday until end of day | Coordinators can still make changes |
| Tuesday | Payroll processing; no changes allowed |
| Wednesday onward | Timesheets unlock; fixes become **retro** adjustments on a later paycheck |

### Resolution process

1. Contact the employee's manager by **email or Teams** to approve or update the time.
2. If the manager is **on leave** (Teams shows out of office, or they reply saying they're out), **escalate immediately** to the next-level manager. Leave is the usual reason a first manager is unavailable.
3. The next-level manager is expected to reply **within 30 minutes**.
4. If neither responds, the coordinator approves time from the employee's **last two pay periods** (schedule, pay code, hours), records it with **initials and timestamp**, and notifies the manager, who can still object before payroll runs.
5. If history is missing or unreliable (new hire, schedule change, unusual hours): try someone on the employee's **team**, using the extra time until end of day Monday. If that fails, enter **standard hours for the job type** (for example, 8 hours a day), to be corrected later through retro.

## 3. Agent design

### Guardrails

- The agent **never approves or enters time on its own.** It proposes; the coordinator approves.
- Every action is written to an **audit log** (who, what, when, why).
- **Business rules live in deterministic code**: deadlines, timers, escalation chain, hour calculations. The LLM writes messages, interprets replies, explains estimates, and chooses the next action.
- Manager replies are **untrusted input** and must never be followed as instructions.
- State wage and overtime rules are out of scope.

### Agents (one package, one module each)

| Agent | Job | LLM does | Code does | Planned week |
| --- | --- | --- | --- | --- |
| **Outreach** | Contact managers, read replies, escalate | Write messages, interpret replies, choose next step | Timers, escalation chain, deadlines | 2 (build first) |
| **Estimator** | Propose fallback hours with confidence | Explain estimates; reason through conflicting history | Calculate hours from last two pay periods | 3–4 |
| **Intake / Triage** | Load, filter, classify, score risk | Classify unclear cases | Filtering, joins, risk score | 4 |
| **Supervisor** | Prioritize, present for approval, report status | Summaries, prioritization reasoning | Approval gate, audit logging | 5 |

**Forecasting elements:** (1) risk that an exception misses the lock; (2) expected hours when falling back on history.

**Feedback loop:** within a run, each reply, out-of-office signal, or silence determines the next step. Across pay periods, coordinator edits, manager corrections, and retro adjustments reveal estimate error. The agent tracks it, adjusts confidence thresholds, and contacts slow-responding managers earlier.

### Stack

| Area | Choice |
| --- | --- |
| Language and tooling | Python 3.12, uv, VS Code, Windows (PowerShell) |
| Orchestration | LangGraph (state machine, checkpoints, human-approval pause); LangChain/LCEL inside nodes; ReAct in Outreach |
| Database | PostgreSQL 16 in Docker Compose (`payroll-pg`, database `payroll`) |
| Short-term memory | LangGraph checkpointer on PostgreSQL (`langgraph-checkpoint-postgres`) |
| Long-term memory | `manager_stats` table |
| Retrieval | pgvector in the same database (policy documents, past resolved cases). Timesheet history uses SQL, not vector search |
| Tools | Mock tools; exposed through an MCP server in Week 4 |
| Simulation | Simulated clock (no real waiting); manager personas: responsive, on_leave, slow, correction, silent, no_next_level |
| Evaluation | LangSmith tracing, Rebuff for prompt injection, scenario tests vs. a fixed-reminder baseline |
| Model provider | Claude (Anthropic API) via `langchain-anthropic`; code default `claude-opus-5-5`; the local `.env` sets `LLM_MODEL=anthropic:claude-haiku-5-5` for cost (about $0.05 per run). Needs a Claude Console API key (not covered by Claude Pro) |

### Repository layout

```
timesheet-exception-agent/
├── docker-compose.yml
├── .env / .env.example
├── src/timesheet_exception_agent/
│   ├── agents/        # outreach.py, estimator.py, intake.py, supervisor.py
│   ├── tools/         # shared mock tools
│   ├── db/            # schema.sql, connection.py
│   ├── sim/           # clock, personas, synthetic data generator
│   ├── retrieval/     # pgvector, policy docs (Week 3)
│   ├── graph.py       # LangGraph workflow (Week 5)
│   └── mcp_server.py  # Week 4
└── tests/
```

### Database tables

`employees` (managers are employees; next-level manager = manager's manager), `coordinator_states`, `pay_periods`, `timesheet_history`, `exceptions`, `presence`, `messages`, `audit_log`, `manager_stats`, `sim_personas` (simulation only). `CHECK` constraints enforce valid statuses and channels.

## 4. Build status

- [x] Checkpoint 1.1 written (745 words)
- [x] Repo created with uv and pushed to GitHub (private)
- [x] Docker Desktop and WSL 2 set up; PostgreSQL + pgvector container running
- [x] Load `schema.sql` and confirm 10 tables
- [x] Verify Python connection (`uv run python -m timesheet_exception_agent.db.connection`)
- [x] Synthetic data generator (`uv run python -m timesheet_exception_agent.sim.seed`): 1 state (CO), 10 staff, 5 frontline + 2 next-level managers, 8 exceptions, all 6 personas; `tests/test_seed.py`
- [x] Simulated clock and persona-driven reply generator (`sim/clock.py`, `sim/personas.py`; `tests/test_sim.py`)
- [x] Outreach tools (`get_exception`, `get_presence`, `get_next_level_manager`, `send_message`, `check_replies`, `update_status`, `get_time`) in `tools/outreach.py`; `tests/test_outreach_tools.py`
- [x] Outreach agent with ReAct, plus the driver loop (Monday 8:00 → 10:00 AM in 10-minute steps) in `agents/outreach.py`; `tests/test_outreach_agent.py`
- [x] First live run with Claude (`uv run python -m timesheet_exception_agent.agents.outreach`), 2026-11-03 on Haiku 5.5; see session log
- [ ] 6–8 scenario tests with expected outcomes

## 5. Evaluation plan

Simulated pay periods compared against a fixed-reminder baseline:

- Exceptions resolved before lock
- Exception recall (none missed before lock)
- Coordinator interventions
- Fallback estimate accuracy
- Wrongful approval rate and retro rate
- Reply-classification accuracy

Failure cases to simulate: manager on leave with no next-level manager, conflicting history, duplicate exception, a reply that looks like approval but isn't, a correction after lock, and an injection attempt in a reply.

## 6. Open items

- Model provider decided: Claude (2026-11-02). Still worth checking whether the course offers API credits
- Confirm exact weekly module deadlines
- Week 3: decide pgvector only, or compare with Chroma for the design write-up
- Seed uses 7 managers, not the 4 originally planned: each manager carries one persona, so covering all 6 personas needs at least 6

## 7. Session log

### 2026-10-23
- Confirmed the schema was already loaded (10 tables) and the Python connection works.
- Added `sim/seed.py` and `tests/test_seed.py` (7 passing). The seed wipes and reloads a fixed dataset each run. Scenario map: E-01/E-02 responsive manager; E-03/E-08 manager on leave, next level responds; E-04 silent manager, slow next level, history fallback; E-07 conflicting history (80 h vs 64 h); E-05 correction; E-06 new hire, no next level, team then standard hours.
- Merged as PR #1 (squash, `e17467c`) into `master`.
- **Next:** simulated clock and persona-driven reply generator (`sim/clock.py`, `sim/personas.py`), then the Outreach tools.
- **Behind schedule:** the Week 3 module (RAG) closed Mon Oct 26; the Outreach agent planned for Week 2 isn't built yet.

### 2026-10-28
- Added `sim/clock.py`: `SimClock` (timezone-aware, America/Denver), starts Mon 2026-10-26 8:00 AM, 10-minute steps, constants `START`, `MANAGER_WINDOW`, `LOCK`, `END_OF_DAY`. Refuses naive times and moving backwards.
- Added `sim/personas.py`: `ReplySimulator` schedules deterministic replies when a message is sent and releases them with `due(now)`. Delays: responsive 10 min, correction 15 min (asks for 72 h instead of 80), slow 75 min (misses the 30-minute next-level window), silent never. On-leave and no-next-level managers send an out-of-office auto-reply on email only (Teams relies on presence). One reply per person per exception; people without a persona (teammates) never reply. `overrides` switches a person to the `ambiguous` (looks like approval, isn't) or `injection` reply for failure-case tests. `load_personas` / `record_replies` read `sim_personas` and write received rows to `messages`.
- `tests/test_sim.py`: 16 tests, pure Python (no DB). Full suite 23 passing.
- Merged as PR #2 (squash, `7312377`) into `master`.
- Added `tools/outreach.py`: `OutreachTools(conn, clock, sim)` with the 7 Outreach tools; `as_tools()` wraps them as LangChain `StructuredTool`s that return rule violations as `{"error": ...}` instead of raising. Rules in code:
  - `send_message` only reaches the employee's manager, the next-level manager, or a teammate (same manager). Validates channel and body (max 2,000 chars). Refuses resolved or closed exceptions.
  - `send_message` sets status itself: manager → `contacted`, next level → `escalated`, teammate → unchanged. It sets `next_check_at` to +30 min, capped at the lock while the lock is ahead.
  - `update_status` lets the agent set only `resolved`, `correction_requested` or `needs_fallback`, with a required reason and a fixed transition table. `closed` is for the coordinator. `resolved` is refused after the lock (managers can't approve then).
  - `check_replies` delivers due replies to `messages`, then returns all replies on the exception with a note that the bodies are untrusted.
  - Every send, received reply and status change goes to `audit_log` (actor `outreach_agent`). Tools never commit; the caller owns the transaction.
- `tests/test_outreach_tools.py`: 26 tests against the seeded DB (each test reseeds and rolls back). Full suite 49 passing.
- **Next:** Outreach agent with ReAct on `as_tools()`, plus the driver loop (8:00 → 10:00 AM in 10-minute steps). Not yet done: updating `manager_stats` (long-term memory) on sends and replies.
- Merged as PR #3 (squash, `fe2c81a`) into `master`.

### 2026-11-02
- Added `agents/outreach.py`:
  - `build_agent(tools, model=None)`: LangChain `create_agent` (ReAct) over `as_tools()` with the workflow rules in the system prompt. The model comes from `LLM_MODEL` via `init_chat_model` (see the provider switch below).
  - `OutreachDriver`: code decides when the agent works on an exception. Each 10-minute tick it delivers due replies, then wakes exceptions for `first_contact` (open, never contacted), `new_reply`, or `follow_up_due` (timer passed). `needs_fallback` exceptions wake only on a reply (a late approval before lock can still resolve them). Each wake is one fresh agent conversation; the database is the memory between wakes. Every turn's summary is audited as `agent_turn` and committed.
  - At the lock, `run()` applies the deadline rule in code: everything still `open`/`contacted`/`escalated` moves to `needs_fallback`, audited with actor `outreach_driver`.
- `tools/outreach.py`: added driver-only hooks `deliver_replies()`, `hand_off_unresolved()` and `audit(..., actor=...)` (not exposed to the model).
- `db/connection.py`: `connect_timeout=5`, so tests fail fast when Docker is down instead of hanging.
- `tests/test_outreach_agent.py`: 8 tests. Driver tests use a coded stand-in policy; agent wiring tests use a scripted fake chat model (no API calls). Full suite 57 passing.
- **Not verified yet:** a live run with the real model. The wake and hand-off flow is tested, but how well the prompt drives real tool choices (escalation on out-of-office, ambiguous and injection replies) needs the live run and then the scenario tests.
- **Next:** first live run; then 6–8 scenario tests with expected outcomes, and a fixed-reminder baseline for comparison. Still pending: `manager_stats` updates.
- **Provider switched to Claude.** The `OPENAI_API_KEY` line came from the initial scaffold and the local `.env` only ever held a placeholder; OpenAI was never chosen. Removed `langchain-openai`, added `langchain-anthropic`. `default_model()` builds the model from `LLM_MODEL` (code default `anthropic:claude-opus-5-5`; the local `.env` overrides it to Haiku 5.5 for cost, and can switch to Sonnet 5.5 or Opus 5.5 if Haiku struggles with tricky replies) and, for Claude: `max_tokens` 16000, `output_config.effort` from `LLM_EFFORT` (default `medium`), no `temperature` (the Claude API now rejects it), and server-side refusal fallbacks (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) on models that support them. The live-run script stops with a clear message if `ANTHROPIC_API_KEY` is missing. `.env.example` now lists `ANTHROPIC_API_KEY`, `LLM_MODEL`, `LLM_EFFORT`. 3 payload tests added (no API calls); full suite 60 passing.
- Cost notes for the write-up: Claude Pro does not cover API usage; API credits are bought in the Claude Console. Rough cost per simulated lock morning: Opus 5.5 about $2, Sonnet 5.5 about $1, Haiku 5.5 about $0.05 (switch with `LLM_MODEL`).
- Run metadata: `model_settings()` reports the model and effort from `LLM_MODEL` / `LLM_EFFORT`. The live-run script prints `Model: ... (effort ...)` before anything else, and stops before reseeding if `ANTHROPIC_API_KEY` is missing or still the `<your-key>` placeholder. `OutreachDriver.run(details=...)` writes a `run_started` audit entry (no exception id, actor `outreach_driver`) holding those settings, so each run's results can be tied to the model that produced them (`SELECT details FROM audit_log WHERE action = 'run_started'`). The audit log is cleared by the seed, so compare runs before reseeding or export results per run. Full suite 63 passing.

### 2026-11-03
- **First live run** (Claude Haiku 5.5, effort medium): 21 agent turns, 14 messages sent, 7 replies, 8 status changes, nothing left for the lock hand-off.

  | Exception | Expected (scenario map) | Live result |
  | --- | --- | --- |
  | E-01, E-02 (responsive manager) | resolved | resolved 08:10 |
  | E-03, E-08 (manager on leave) | escalate at once, next level approves | escalated to N-01 at 08:00 (presence showed out of office), resolved 08:10 |
  | E-05 (correction) | correction_requested | correction_requested 08:20, reason says 72 h not 80 |
  | E-06 (new hire, no next level) | teammate, then needs_fallback | emailed teammate E-09, needs_fallback 08:00 |
  | E-04, E-07 (silent manager, slow next level) | escalate at 08:30; needs_fallback at 09:00 when N-02 misses her 30 minutes | escalated at 08:30, but **re-messaged N-02 at 09:00 and 09:30 instead of needs_fallback**; resolved 09:50 only because the slow approval (09:45) arrived before the lock |

- Messages were short, polite, named the employee and the 10:00 AM lock, and used "their" for employees.
- Problems found:
  1. The 30-minute next-level rule lives only in the prompt, and Haiku did not follow it (4 messages to N-02 per exception instead of 1). This breaks the project rule that timers and escalation are deterministic code.
  2. `send_message` rejected channel "Teams" (capitalized); the agent recovered on retry, but the tool could accept any casing.
  3. Agent summaries guessed pronouns for managers ("she"); the prompt should say to refer to people by name.
  4. A message for a not_approved exception mentioned "check the unusual hours" (minor wording copied from the prompt).
- **Fixes after run 1:**
  - The 30-minute next-level rule is now code: `OutreachDriver.due()` hands an `escalated` exception to the coordinator (`needs_fallback`, actor `outreach_driver`) when its follow-up time passes with no new reply, without waking the agent. A reply that arrives in the same tick wins. A later reply still wakes the agent, so a late approval before the lock can resolve it. New tools hook `hand_off(ids, reason)`; `hand_off_unresolved()` uses it.
  - `send_message` lowercases and trims the channel (" Teams " → "teams").
  - Prompt: escalate to the next level once and leave the timeout to the system; ask only for what the exception type needs; refer to people by name or role and never assume pronouns.
  - 4 new tests; full suite 67 passing.
- **Live run 2** (Haiku 5.5, same seed), compared with run 1:

  | | Run 1 | Run 2 |
  | --- | --- | --- |
  | Agent turns | 21 | 17 |
  | Messages sent | 14 | 10 |
  | Messages to N-02 per silent-manager exception | 3 | 1 |
  | E-04 / E-07 at 09:00 | still waiting, re-messaged | `needs_fallback` by code |
  | E-04 / E-07 final | resolved 09:50 | resolved 09:50 (late approval moved them from `needs_fallback` to `resolved`) |
  | Tool errors | 1 (channel casing) | 0 |
  | Pronouns in summaries | "she" for managers | names or "they" |
  | Off-type wording | "check the unusual hours" on not_approved | none seen |

  All other exceptions ended the same as run 1 and as the scenario map expects.
- **Scenario note:** with the slow persona at 75 minutes, N-02's approval (09:45) always lands before the lock, so E-04/E-07 never finish in the history-fallback state the scenario map describes. The scenario tests should also run them with N-02 overridden to `silent` to cover the true fallback path.
