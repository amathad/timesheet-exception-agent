# Project Context: Timesheet Exception Agent

Capstone project for the CMU School of Computer Science program *Agent AI Development for Real World Applications* (Sept–Nov 2026). This file records the problem, the rules the agent must follow, the design decisions made so far, and where the build stands. Update it at the end of each working session.

**Capstone option:** Forecaster
**Status as of 2026-11-04:** Module 2 done: Outreach agent built and tested live on Claude Haiku 5.5. Module 3 (RAG) in progress: corpus, pgvector store, hybrid search and evaluation built; agent tools next.

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
   - If the manager keeps replying but hasn't approved or corrected by **9:00 AM** (for example, stuck in meetings), escalate to the next-level manager at 9:00. Both managers get ample time, and the next-level manager still has their 30 minutes before the lock. (Decided 2026-11-04.)
   - A manager who has replied is not escalated before 9:00 AM; they get reminders instead. Silent and out-of-office managers can still be escalated earlier. (Decided 2026-11-04; enforced in `send_message`.)
4. If neither responds, the coordinator approves time from the employee's **last two pay periods** (schedule, pay code, hours), records it with **initials and timestamp**, and notifies the manager, who can still object before payroll runs.
5. If history is missing or unreliable (new hire, schedule change, unusual hours): try someone on the employee's **team**, using the extra time until end of day Monday. If that fails, enter **standard hours for the job type** (for example, 8 hours a day), to be corrected later through retro.

## 3. Agent design

### Guardrails

- The agent **never approves or enters time on its own.** It proposes; the coordinator approves.
- Every action is written to an **audit log** (who, what, when, why).
- At most **3 messages to the same person** about one exception (the first message plus two reminders). (Decided 2026-11-04.)
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
| Retrieval | pgvector in the same database (policy documents, past resolved cases), hybrid with PostgreSQL full-text search via reciprocal rank fusion. Timesheet history uses SQL, not vector search |
| Embeddings | `BAAI/bge-small-en-v1.5` through fastembed, local CPU, 384 dimensions, no API key (decided 2026-11-04). Claude has no embedding endpoint |
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
│   ├── retrieval/     # policies/*.md, cases.py, embeddings, ingest, search, eval
│   ├── graph.py       # LangGraph workflow (Week 5)
│   └── mcp_server.py  # Week 4
└── tests/
```

### Database tables

`employees` (managers are employees; next-level manager = manager's manager), `coordinator_states`, `pay_periods`, `timesheet_history`, `exceptions`, `presence`, `messages`, `audit_log`, `manager_stats`, `sim_personas` (simulation only). `CHECK` constraints enforce valid statuses and channels.

Retrieval tables (12 in total): `policy_chunks` and `case_memory`, each with a `vector(384)` embedding (HNSW), a generated `tsvector` (GIN), and the embedding `model` that wrote it. They're loaded by `retrieval/ingest.py` from files, not by the seed, so reseeding keeps them; `case_memory` stores employee ids as plain text, not foreign keys, for the same reason.

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
- [x] First live run with Claude (`uv run python -m timesheet_exception_agent.agents.outreach`), 2026-11-03 on Haiku 5.5; see `docs/LIVE_RUNS.md`
- [x] Outreach scenario tests with expected outcomes (silent and slow next level, stalling, ambiguous, injection) in `tests/test_outreach_agent.py`, plus live runs in `docs/LIVE_RUNS.md`. Duplicate exception and correction after lock are deferred to Intake/Triage and Supervisor
- [x] Module 3 retrieval, PR (a): 9 synthetic policy documents (36 chunks) and 21 past cases; `policy_chunks` and `case_memory` tables; local fastembed embeddings; ingest (`uv run python -m timesheet_exception_agent.retrieval.ingest`); hybrid search; gold-set evaluation (`retrieval.eval`, results in `docs/RETRIEVAL_EVAL.md`); `tests/test_retrieval.py`
- [ ] Module 3 retrieval, PR (b): `search_policy` and `find_similar_cases` tools for Outreach, prompt update to cite sources, live run

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
- Week 3: decide pgvector only, or compare with Chroma for the design write-up. Recommendation: pgvector only (one database, SQL joins with timesheet history), explained in the write-up
- Seed uses 7 managers, not the 4 originally planned: each manager carries one persona, so covering all 6 personas needs at least 6

## 7. Session log

### 2026-10-23 (Module 1)
- Confirmed the schema was already loaded (10 tables) and the Python connection works.
- Added `sim/seed.py` and `tests/test_seed.py` (7 passing). The seed wipes and reloads a fixed dataset each run. Scenario map: E-01/E-02 responsive manager; E-03/E-08 manager on leave, next level responds; E-04 silent manager, slow next level, history fallback; E-07 conflicting history (80 h vs 64 h); E-05 correction; E-06 new hire, no next level, team then standard hours.
- Merged as PR #1 (squash, `e17467c`) into `master`.
- **Next:** simulated clock and persona-driven reply generator (`sim/clock.py`, `sim/personas.py`), then the Outreach tools.
- **Behind schedule:** the Week 3 module (RAG) closed Mon Oct 26; the Outreach agent planned for Week 2 isn't built yet.

### 2026-10-28 (Module 2)
- Added `sim/clock.py` (`SimClock`: lock-day clock from Mon 2026-10-26 8:00 AM Mountain, 10-minute steps) and `sim/personas.py` (`ReplySimulator`: deterministic replies per persona, plus `ambiguous` and `injection` overrides); `tests/test_sim.py` (16, no DB).
- Merged as PR #2 (squash, `7312377`) into `master`.
- Added `tools/outreach.py`: the 7 Outreach tools in `OutreachTools`, with recipients, status changes, follow-up timers and the lock enforced in code, and every action written to `audit_log`; `tests/test_outreach_tools.py` (26). Full suite 49 passing.
- Merged as PR #3 (squash, `fe2c81a`) into `master`.
- **Next:** Outreach agent with ReAct, plus the driver loop (Monday 8:00 → 10:00 AM in 10-minute steps).

### 2026-11-02 (Module 2)
- Added `agents/outreach.py`: ReAct agent (`build_agent`) and `OutreachDriver`, which wakes the agent per exception (first contact, new reply, follow-up due) and hands anything unresolved to the coordinator at the lock; `tests/test_outreach_agent.py` (scripted fake model, no API calls). `db/connection.py` now has `connect_timeout=5`.
- Model provider decided: Claude via `langchain-anthropic`, replacing the unused OpenAI scaffold. Code default `claude-opus-5-5`; the local `.env` sets `LLM_MODEL=anthropic:claude-haiku-5-5` for cost. No `temperature` (the Claude API rejects it). `.env.example` lists `ANTHROPIC_API_KEY`, `LLM_MODEL`, `LLM_EFFORT`. Full suite 63 passing.
- **Next:** first live run.

### 2026-11-03 (Module 2)
- First two live runs on Haiku 5.5; results and fixes in `docs/LIVE_RUNS.md` (runs 1 and 2). The 30-minute next-level timeout moved from the prompt into `OutreachDriver.due()`; `send_message` accepts any channel casing; prompt rules on names and wording. Full suite 67 passing.
- **Next:** test the true fallback path with a silent next-level manager.

### 2026-11-04 (Module 2)
- Added `--override PERSON=KIND` to the live-run script (`agents/outreach.py`) and the `stalling` reply kind (`sim/personas.py`).
- Rules decided with the user (sections 2 and 3), enforced in code: the 9:00 AM escalation cut-off (`ESCALATION_CUTOFF` and early-escalation refusal in `tools/outreach.py`, `escalation_due` wake in `agents/outreach.py`) and at most 3 messages per person per exception.
- Added `OutreachTools.scoped`, so a reply can't steer the agent into other exceptions.
- Six more live runs (silent next level, stalling, ambiguous, injection); all pass after the fixes. See `docs/LIVE_RUNS.md` (runs 3, A–E). Full suite 93 passing.
- Merged as PR #4 (squash, `9304137`) and PR #5 (squash, `586bc5a`) into `master`; `.vscode/` added to `.gitignore` (`6986bdf`).
- **Next:** Module 3 (RAG with pgvector: policy documents and past resolved cases in `retrieval/`). Deferred: duplicate exception (Intake/Triage, Week 4), correction after lock (Supervisor, Week 5), fixed-reminder baseline (evaluation), `manager_stats` updates.
- **Behind schedule:** Module 3 (RAG) closed Mon Oct 26 and isn't started.

### 2026-11-04 (Module 3)
- Embedding provider decided: local fastembed (`BAAI/bge-small-en-v1.5`, 384 dimensions), so retrieval needs no extra key or cost.
- Added `retrieval/`: 9 synthetic policy documents in `policies/` (7 coordinator-handbook documents and 2 distractors), 21 past resolved cases in `cases.py` that match the seed's people, personas and history, `embeddings.py` (`FastEmbedder`, plus `HashEmbedder` for tests), `ingest.py`, `search.py` (pgvector + full-text, reciprocal rank fusion, source citations, refuses vectors from a different model), and `eval.py`.
- Schema: `policy_chunks` and `case_memory` (12 tables). The seed leaves them alone.
- Evaluation on 18 policy and 7 case questions: hybrid policy recall@3 0.83 (vector 0.78, keyword 0.72), but vector has the better hit@1 (0.72 vs 0.56). Equal-weight fusion kept; see `docs/RETRIEVAL_EVAL.md`. Full suite 110 passing.
- Merged as PR #6 (squash, `c829675`) into `master`.
- **Next:** PR (b): Outreach retrieval tools, prompt citations, live run.
