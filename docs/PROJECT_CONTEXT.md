# Project Context: Timesheet Exception Agent

Capstone project for the CMU School of Computer Science program *Agent AI Development for Real World Applications* (Sept–Nov 2026). This file records the problem, the rules the agent must follow, the design decisions made so far, and where the build stands. Update it at the end of each working session.

**Capstone option:** Forecaster
**Status as of 2026-11-24:** Module 2 done: Outreach agent built and tested live on Claude Haiku 5.5. Module 3 (RAG) done: pgvector hybrid retrieval over policies and past cases, wired into Outreach as cited, audited tools and tested live. Module 4 done: the minimal code-only Estimator is merged and tested live, and a read-only MCP server exposes the run to any MCP client. Module 5 done: `graph.py` (LangGraph lock-day workflow with parallel Outreach and a coordinator approval pause), code-only Intake, the rule-based router, the fixed-reminder baseline, merged delivery and the LLM Supervisor are built and tested live. The Supervisor A/B against the router is done (63 runs): the Supervisor changed no outcome, so the router stays the default. Gated ToT in the Estimator with the Supervisor send-back (closing Checkpoint 5.1) and hard state/date filters on policy search are built and tested live (PR #12). Module 6 built: metrics over the audit log for every Checkpoint 6.1 group, a deterministic reply screen, refusal auditing, guardrail switches and an ablation, and LangSmith tracing (tested live, runs LS1–LS2); evaluated over 32 live runs (`docs/EVAL_RESULTS.md`). `README.md` written. Module 7 done: the repo is public, the MCP server was tested from Claude Desktop, and the final report with the presentation and elevator-pitch videos was submitted by the 2026-11-23 deadline.

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
- Every action is written to an **audit log** (who, what, when, why), including every retrieval call with its query and sources. (Decided 2026-11-05.)
- Retrieved policy and cases are **reference material**: the agent cites a policy section only in status reasons, never in messages to managers, and `update_status` refuses any citation that retrieval didn't return for that exception. (Decided 2026-11-05.)
- On every wake that follows a reply, **code** retrieves the policy for that reply and gives it to the agent; whether pay history can be copied is decided in code (`history_check`). (2026-11-05.)
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
| Tools | Mock tools, called in-process as LangChain tools by the agents. A read-only MCP server (`mcp_server.py`, official `mcp` SDK v2, stdio) exposes the run to MCP clients (decided 2026-11-18: no write tools) |
| Simulation | Simulated clock (no real waiting); manager personas: responsive, on_leave, slow, correction, silent, no_next_level |
| Evaluation | `metrics.py` over the audit log, scenario tests vs. a fixed-reminder baseline, guardrail ablation, LangSmith tracing (optional, `.env`). A deterministic reply screen (`tools/guardrails.py`) replaced Rebuff (decided 2026-11-18: no model call, and a reply can't talk it out of a flag) |
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
│   ├── ab.py          # Supervisor A/B against the router and the baseline
│   └── mcp_server.py  # read-only MCP server (Module 4)
└── tests/
```

### Database tables

`employees` (managers are employees; next-level manager = manager's manager), `coordinator_states`, `pay_periods`, `timesheet_history`, `exceptions`, `presence`, `messages`, `audit_log`, `manager_stats`, `sim_personas` (simulation only). `CHECK` constraints enforce valid statuses and channels.

Retrieval tables (12 in total): `policy_chunks` and `case_memory`, each with a `vector(384)` embedding (HNSW), a generated `tsvector` (GIN), and the embedding `model` that wrote it. They're loaded by `retrieval/ingest.py` from files, not by the seed, so reseeding keeps them; `case_memory` stores employee ids as plain text, not foreign keys, for the same reason.

Estimator table (13 in total): `estimates`, one fallback proposal per exception (path, method, hours, pay code, confidence, reason, basis, status `proposed`/`withdrawn`/`approved`/`edited`/`rejected`). `approved_by` and `approved_at` stay empty until the coordinator's approval gate (`agents/approval.py`), which also writes `final_hours` and `final_pay_code`, so a proposal's error can be measured. Gated ToT adds `runner_up`, `tot` (the reasoning record) and `sent_back`. The seed clears it. `policy_chunks` also has `states`, `effective_from` and `effective_to`, from each policy file's header, for the hard search filters.

Triage table (14 in total): `triage`, one row per exception (manager, lock risk 0–1 with its `factors`, `duplicate_of`), written by Intake and rescored by the router each tick. Exceptions have a new status, `duplicate`. Outbox table (15 in total): `outbox`, Outreach drafts queued during a tick in graph runs, delivered (and merged) at its end; `messages.batch_id` links the per-exception rows of one merged message; `estimates.flag` holds a Supervisor concern. `PostgresSaver.setup()` adds four LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`), for 19 in the database.

## 4. Build status

- [x] Checkpoint 1.1 written (745 words)
- [x] Repo created with uv and pushed to GitHub (private; made public for grading by 2026-11-23)
- [x] Docker Desktop and WSL 2 set up; PostgreSQL + pgvector container running
- [x] Load `schema.sql` and confirm 10 tables
- [x] Verify Python connection (`uv run python -m timesheet_exception_agent.db.connection`)
- [x] Synthetic data generator (`uv run python -m timesheet_exception_agent.sim.seed`): 1 state (CO), 10 staff, 5 frontline + 2 next-level managers, 8 exceptions, all 6 personas; `tests/test_seed.py`
- [x] Simulated clock and persona-driven reply generator (`sim/clock.py`, `sim/personas.py`; `tests/test_sim.py`)
- [x] Outreach tools (`get_exception`, `get_presence`, `get_next_level_manager`, `send_message`, `check_replies`, `update_status`, `get_time`) in `tools/outreach.py`; `tests/test_outreach_tools.py`
- [x] Outreach agent with ReAct, plus the driver loop (Monday 8:00 → 10:00 AM in 10-minute steps) in `agents/outreach.py`; `tests/test_outreach_agent.py`
- [x] First live run with Claude (`uv run python -m timesheet_exception_agent.agents.outreach`), 2026-11-03 on Haiku 5.5; see `docs/LIVE_RUNS.md`
- [x] Outreach scenario tests with expected outcomes (silent and slow next level, stalling, ambiguous, injection) in `tests/test_outreach_agent.py`, plus live runs in `docs/LIVE_RUNS.md`. Duplicate exception and correction after lock are deferred to Intake/Triage and Supervisor
- [x] Module 3 retrieval, PR (a): 9 synthetic policy documents (36 chunks; 37 after PR (b)) and 21 past cases (22 after PR (b)); `policy_chunks` and `case_memory` tables; local fastembed embeddings; ingest (`uv run python -m timesheet_exception_agent.retrieval.ingest`); hybrid search; gold-set evaluation (`retrieval.eval`, results in `docs/RETRIEVAL_EVAL.md`); `tests/test_retrieval.py`
- [x] Module 3 retrieval, PR (b): `search_policy` and `find_similar_cases` in `tools/retrieval.py` (audited), wired into Outreach with a citation prompt (`--no-retrieval` for the Module 2 agent), citation guard in `update_status`, `citation_report`; `tests/test_retrieval_tools.py`; live runs F–H in `docs/LIVE_RUNS.md`; `use_history` A/B scenario (runs I and J) where retrieval changes the handling; fixes after run I (driver retrieves policy for every reply, `history_check` in code, citations must be retrieved for the exception) checked in runs K–P
- [x] Module 4 minimal Estimator (code only, no model, no ToT) in `agents/estimator.py`: for each exception at `needs_fallback`, proposes hours, pay code and confidence from the last two closed pay periods (`history_check` plus a pay-code check); history that fails goes to the teammate path with standard hours for the job type as the backup (standard hours alone if no teammate can be asked; teammates with an open exception of their own are excluded). Proposals only, in the new `estimates` table (13 tables); audited as actor `estimator`; withdrawn if a manager decides later. Hooked into `OutreachDriver(..., estimator=...)` and the live-run script; `tests/test_estimator.py` (26). Full suite 161 passing
- [x] Module 5 Intake (`agents/intake.py`, code only): loads the coordinator's exceptions for the period, marks a second exception on the same employee and period `duplicate`, groups by manager, and scores lock risk (forecasting element 1) from presence, the escalation chain, history, `manager_stats`, messages and the clock, never `sim_personas`; new `triage` table (14 tables); `tests/test_intake.py`
- [x] Module 5 rule-based router (`agents/router.py`, code only; the Supervisor's fallback and A/B baseline): rescores risk each tick, wakes a manager's other contacted exceptions with `manager_out` once the manager is known to be out (presence or an auto-reply), orders wakes by risk, and builds the approval queue (lowest confidence first); `OutreachDriver(..., plan=...)`; `tests/test_router.py`
- [x] Fixed-reminder baseline (`agents/baseline.py`): emails the manager, fixed reminders up to the cap, never escalates, resolves on the keyword "approv"; `tests/test_baseline.py`
- [x] `graph.py`: LangGraph workflow (intake → tick/advance loop → lock → approval `interrupt` → report), `PostgresSaver` checkpoints, coordinator approval gate (`agents/approval.py`: approve/edit/reject with initials and time, audited as `coordinator`); `--baseline`, `--duplicate`, `--decisions`; `tests/test_graph.py`. Estimator now records teammates already contacted (run Q finding). Live runs R–T in `docs/LIVE_RUNS.md`. Full suite 205 passing
- [x] Parallel Outreach (`graph.py`): wakes fanned out with LangGraph `Send` (`max_concurrency` 4, `--sequential` for 1), each task with its own connection, `OutreachTools` and scope; thread-safe reply simulator
- [x] Merged delivery (`tools/outbox.py`): `send_message` queues drafts in graph runs; at the end of the tick, drafts to the same person and channel are merged (router template, or the Supervisor's body) after code checks (every employee in the group named, no one else, no citations, length), else sent separately; one reply to a merged message is filed on each exception, and `check_replies` marks it
- [x] Leave replies: Outreach tool `record_manager_leave` (tied to the manager's reply, audited) and email auto-replies now allow escalation before 9:00 AM, per rule 2; new reply kind `on_leave_teams`
- [x] LLM Supervisor (`agents/supervisor.py`): coordinates across exceptions over code orchestration, called on events (review), for merges and at the lock (queue); tools `get_overview`, `escalate_group`, `wake_now`, `flag_estimate`, `merge_messages`, `write_queue`, each refusing illegal moves in code; the router's plan stands if a call fails; calls and tokens audited. `tests/test_outbox.py`, `tests/test_supervisor.py`; live runs U1–W2. Full suite 244 passing
- [x] Supervisor A/B against the router (`ab.py`): 9 scenarios (new reply kind `late`), 3 runs each for the router and Supervisor arms and 1 for the baseline, metrics read from the database at the lock; Outreach tokens now audited per turn; `graph.run_lock_day` shared by the CLI and the A/B. The Supervisor matched the router on every exception in every run at 17% more tokens and twice the wall time, so the router stays the default (`docs/AB_RESULTS.md`, runs AB1–AB63 in `docs/LIVE_RUNS.md`); `tests/test_ab.py`. Full suite 262 passing
- [x] Policy search with hard state and effective-date filters (Checkpoint 6.1): every policy document states `States:`/`Effective:`/`Until:`; `search_policy` and `policy_for_reply` always filter on the employee's state and the simulated date, and audit the filters; two out-of-scope distractors (`escalation_ut`, `escalation_2025`). Without filters 3 of 19 gold questions put a Utah or 2025 rule in the top 3; with them 0, at unchanged recall@3 0.84 (`docs/RETRIEVAL_EVAL.md`)
- [x] Gated Tree-of-Thought in the Estimator (`agents/tot.py`, closes Checkpoint 5.1): runs only when history fails its check and code can build two or more candidates (each closed period, standard hours); the model scores candidates, a critic rescores the best two, code picks the recommendation and runner-up and sets confidence (0.30–0.60); the explain step self-checks (`agrees`), and a split goes to the policy-safe candidate at minimum confidence and is flagged; 12-call budget per exception; evidence includes the employee's own past cases by SQL. Supervisor send-back: `flag_estimate` on a ToT proposal reruns it once with the concern and flags it only if the recommendation stands. `estimates.runner_up`, `tot`, `sent_back`; runner-up shown in the approval queue; `--no-tot`. `tests/test_tot.py` (21). Live runs X1–X4. Full suite 291 passing
- [x] Module 6 evaluation (`metrics.py`): Checkpoint 6.1 metrics read from the database after each run (correctness incl. reply-classification accuracy and fallback hour error against known answers, groundedness, calibration with a scripted known-answer coordinator, safety, efficiency incl. cost); JSON lines in `docs/eval/`, `--summary` tables. Every refused tool call is audited (`tool_refused` with its guardrail). Deterministic reply screen (`tools/guardrails.py`): a flagged reply is not a decision (10 of 12 labelled injections, 0 of 11 false positives). Guardrail switches (`Guardrails`, `--off`) audit `guardrail_bypassed`; `--ablation`. LangSmith run names, tags and metadata (`LANGSMITH_*` in `.env`), checked live in runs LS1–LS2; the morning trace no longer ends as an error at the approval pause. Fix found by the evaluation: a reply is matched to the message it answers by id, not only time. 32 live runs (EV1–EV32, $1.27): all targets met; ablation: nothing got through with any guardrail off; standard-hours confidence too low on this seed (`docs/EVAL_RESULTS.md`). `tests/test_guardrails.py` (29). Full suite 321 passing
- [x] Module 4 MCP server (`mcp_server.py`, official `mcp` SDK v2, stdio): read-only by design (no tool sends, changes a status or approves), 7 tools reusing the agents' code (`list_exceptions`, `get_exception`, `get_messages` with untrusted, screened replies, `search_policy` with the state/date filters, `find_similar_cases`, `get_approval_queue`, `get_audit_trail`); every call audited as actor `mcp`, refusals as `tool_refused`. Smoke-checked over stdio against a baseline morning (run MCP1). Connection guide for the MCP Inspector, Claude Code and Claude Desktop in `docs/MCP_SERVER.md`; `tests/test_mcp_server.py` (9, in-memory MCP session). Full suite 330 passing

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
- Module deadlines (resolved 2026-11-12): modules have no hard deadlines of their own; everything must be complete by the course deadline. Build order follows the design, not the module numbers
- **Course deadline: Mon 2026-11-23** for the complete project, with the final report and video presentation (recorded 2026-11-16). An extension of a few days can be requested, but submitting early keeps the chance of being picked for the top presentations at the final session on Wed 2026-11-25. Target: build, final report and video done by Thu 2026-11-19, then a final review and submission on Mon 2026-11-23
- Week 3: decide pgvector only, or compare with Chroma for the design write-up. Recommendation: pgvector only (one database, SQL joins with timesheet history), explained in the write-up
- Seed uses 7 managers, not the 4 originally planned: each manager carries one persona, so covering all 6 personas needs at least 6
- Retrieval ranking (2026-11-05): the best rule for E-06 ranks outside the top 3, and 3 of 19 gold-set policy questions miss. Consider a reranker once the gold set is larger

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

### 2026-11-05 (Module 3)
- Defaults confirmed with the user: every retrieval call is audited, and citations stay out of messages to managers.
- Added `tools/retrieval.py`. `search_policy` returns the top 3 sections with `[doc > section]` citations. `find_similar_cases` builds its query from the exception's facts in code and never reads the sim personas. Both tools are audited, and their results are marked as reference material.
- Wired into the Outreach agent through `build_agent(..., retrieval=...)` and `RETRIEVAL_PROMPT`. The live-run script checks the corpus is loaded, and `--no-retrieval` runs the Module 2 agent.
- Three live runs on Haiku (F, G, H in `docs/LIVE_RUNS.md`). Outcomes were unchanged from Module 2; hand-off and correction reasons cited retrieved sections; no citations appeared in messages to managers. Run F produced one made-up citation, now refused in code by `update_status`.
- The user pointed out that runs F–H didn't really test retrieval, since the outcomes matched Module 2. Added a scenario where the rule is only in the handbook and the deciding facts only come through retrieval: the `use_history` reply kind (the manager asks the coordinator to "just use what they usually work"), a new policy section, case P3-07, and a `facts` field in `find_similar_cases` with raw history. A/B on Haiku: with retrieval, E-04/E-07 were handed off at 08:20–08:30 with the rule cited and E-07's 64/80 conflict flagged. Without retrieval, the agent sent 3 reminders and 2 escalations and the code handed off at 09:30 with a generic reason (runs I and J). The first wording of the new section lowered policy recall@3 to 0.79, so a shorter wording was kept (0.84). Full suite 127 passing.
- Fixed the run I findings. The driver retrieves policy for every reply wake (`policy_for_reply`); `history_check` decides in code whether history can be copied and cites the rule; the similar-case query includes the latest reply; the description of `send_message` says sending sets the status. Over 3 `use_history` runs (K–M) both exceptions were handed off on the first turn after the reply, with the unreliable-history rule cited and P3-01 mentioned.
- Run K cited a section it hadn't retrieved: the prompt's own example. The prompt now shows only the form, and `update_status` refuses citations not retrieved for that exception. Runs O and P were clean. Full suite 135 passing. Runs K to P are in `docs/LIVE_RUNS.md`.
- Merged as PR #7 (squash, `569e699`) into `master`. Module 3 done.
- Open: the best rule for E-06 ranks outside the top 3, and 3 gold-set questions miss (`docs/RETRIEVAL_EVAL.md`). Both need a reranker and a larger gold set.
- **Next:** Module 4: Estimator (hours from the last two pay periods in code, building on `history_check`, with `find_similar_cases` feeding its explanations). Still deferred: duplicate exception (Intake/Triage, Week 4), correction after lock (Supervisor, Week 5), fixed-reminder baseline (evaluation), `manager_stats` updates.
- **Behind schedule:** Module 3 (RAG) closed Mon Oct 26 and was finished Nov 5. Confirm the Module 4 deadline (open item).

### 2026-11-10 (Module 5, design only)
- Wrote and submitted Checkpoint 5.1 (Multi-Agent Architecture and Coordination Plan), checked against the repo. No code changes.
- Design decided with the user:
  - Three LLM agents (Outreach built; Estimator and Supervisor planned), plus Intake/Triage as code only (dedupe, group exceptions by manager, lock-risk score).
  - Supervisor is an LLM agent with a fixed tool set: apply facts across a manager's exceptions (escalate all at once), merge messages to the same manager, reprioritize near the lock, check each estimate against the manager's replies (at most one send-back), write the approval queue. Tools refuse illegal moves (after lock, over message caps, approving time). Called only on events (reply, hand-off, new estimate).
  - A rule-based router is both the fallback and the baseline: keep the LLM Supervisor only if it beats the router on the same scenarios enough to justify calls and latency.
  - LangGraph graph advances one shared simulated clock in 10-minute steps; exceptions that need attention at a step run Outreach in parallel. Typed state, checkpointed to PostgreSQL; coordinator approval pause.
- Open: per-manager batching changes Outreach's per-exception scoping and the 3-messages-per-person cap; keep the scoping protection (one interpretation and status per exception).
- **Next:** minimal Estimator (hours from last two pay periods via history_check, no ToT) → graph.py with Intake and the rule-based router → LLM Supervisor and A/B vs router → gated ToT in Estimator → MCP server.
- **Behind schedule:** Module 4 build (Estimator, ToT, MCP server) not started; Checkpoints 4.1 and 5.1 submitted as designs.

### 2026-11-12 (Module 4)
- Added the 2026-11-10 entry above, as written by the user.
- Defaults confirmed with the user: the minimal Estimator is code only, and the model's part (explaining estimates, reasoning through conflicting history) comes with gated ToT later. The teammate path is recorded on the estimate, so the exception stays at `needs_fallback`. Confidence values 0.95 / 0.90 / 0.80 / 0.30 are placeholders for the feedback loop to tune.
- Added `agents/estimator.py`.
  - Pure rules: `decide`, `pay_code_check`, `history_confidence`, `standard_hours`, prorated over weekdays from the hire date.
  - `Estimator.sync`, called by `OutreachDriver(..., estimator=...)` after each tick and after the lock hand-off. It proposes for every exception at `needs_fallback` and withdraws a proposal if a manager decides later.
  - Paths: `history` (the last two periods pass `history_check` and have the same pay code), `teammate` with standard hours as the backup, or `standard_hours` alone when no teammate can be asked.
  - The new `estimates` table (13 tables): proposals only, never approved, and each proposal, withdrawal and skip is audited as actor `estimator`. The live-run script prints the proposals.
- After reviewing the diff, the user decided to exclude teammates who have an open exception of their own. E-07's teammates are now only E-10; E-04 is kept in the basis as excluded. `tests/test_estimator.py` (26, no API calls). Full suite 161 passing.
- Live run Q on Haiku (`N-02=silent`, in `docs/LIVE_RUNS.md`). Outreach outcomes matched runs 3 and G. E-04 got the history path (80 h at 0.95); E-07 and E-06 got the teammate path (80 h standard at 0.30). Nothing was approved and no status was changed by the Estimator. Printed hours fixed from `80.00` to `80`.
- Merged as PR #8 (squash, `b0ef35b`) into `master`.
- Open: for E-06 the Estimator still says to ask E-09, although Outreach already emailed E-09 (the Estimator doesn't read `messages`). The default-morning withdrawal hasn't been run live; a test covers it. The repo has no CI, so tests run only locally.
- Confirmed with the user: modules have no hard deadlines of their own, only the course deadline for the whole project. So gated ToT stays after `graph.py` and the Supervisor, as planned on 2026-11-10, and the deadline open item is closed.
- **Next:** `graph.py` with Intake and the rule-based router, then the LLM Supervisor and its A/B against the router, then gated ToT in the Estimator, then the MCP server.
- **Behind schedule:** not against module dates, which aren't binding. Still to build before the course deadline: `graph.py` and Intake, the Supervisor, gated ToT, and the MCP server. Checkpoints 4.1 and 5.1 were submitted as designs.

### 2026-11-16 (Module 5)
- Plan agreed with the user before building. Defaults confirmed:
  - a `duplicate` status;
  - Outreach wakes run one after another within a tick, in router order, not in parallel (shared connection, scope guard and reply simulator);
  - checkpoints resume the approval pause, not a morning halfway;
  - the Estimator reads `messages` for teammates already asked.
- Work on branch `module5-graph-intake-router`.
- Added `agents/intake.py`. Intake loads the period, dedupes, groups by manager and scores lock risk. The risk weights are placeholders for the feedback loop, and every term is stored in `triage.factors`. Schema: the `triage` table, the `duplicate` status (the status CHECK is now applied with `ALTER`, so existing databases pick it up on the next seed), and `estimates.final_hours` / `final_pay_code`. `sim/seed.py` has `add_duplicate()` (not seeded by default).
- Added `agents/router.py`. Each tick it rescores, adds `manager_out` wakes once per exception, orders wakes by risk, and builds the approval queue. It is hooked in through `OutreachDriver(..., plan=...)`. The driver's lock step is now `OutreachDriver.lock()`, and the new `manager_out` wake reason is in `REASONS`.
- Added `agents/baseline.py` (fixed-reminder baseline) and `agents/approval.py` (coordinator approval gate: approve, edit or reject, with initials and time; approve and edit close the exception).
- Added `graph.py`: the LangGraph `StateGraph` with typed state, a `PostgresSaver` checkpointer, and an approval `interrupt` resumed with `Command(resume=decisions)`.
- Estimator: teammates already messaged go in `teammates_contacted`, and the proposal says to wait for their answer (closes the run Q finding). A `duplicate` exception no longer excludes a teammate.
- New tests: `test_intake.py`, `test_router.py`, `test_baseline.py`, `test_graph.py`, plus 2 in `test_estimator.py`. Full suite 205 passing.
- Live runs on Haiku R (default), S (baseline, default) and T (`--duplicate --override N-02=silent`); see `docs/LIVE_RUNS.md`.
  - On the same morning, the agent got 7 manager decisions before the lock with 10 messages. The baseline got 3 with 18 messages, and one of its 3 was wrong (E-05's correction taken as an approval).
  - First live withdrawal of proposals after a late approval (R).
  - The coordinator's edit, reject and approve were all recorded with initials (T).
  - Fixed during the runs: a byte-order mark on piped initials, and risk not rescored after approvals.
- Open:
  - The router's `manager_out` rule didn't fire with the agent, which already escalates at first contact; it is covered by tests only.
  - The baseline's turn count includes no-op wakes after the cap.
  - Wakes within a tick are still sequential.
  - The approval gate takes decisions on the console or from a file; the LLM Supervisor will own the queue.
- Merged as PR #9 (squash, `95402e0`) into `master`.
- **Next:** LLM Supervisor with message merging (show the scoping design before coding it), then its A/B against the router.
- **Behind schedule:** on plan for the 2026-11-16 slot (graph, Intake, router, baseline). Still to build: the LLM Supervisor, the A/B, gated ToT, the MCP server, then the evaluation tables, report and video.

### 2026-11-17 (Module 5)
- Design agreed with the user before building:
  - **The Supervisor's role:** the graph (code) is the orchestrator. The Supervisor is an LLM agent that coordinates across exceptions over the router's plan, not an LLM that picks the next agent. That keeps business rules in code and isolates what the LLM adds for the A/B. The name is kept, and the report will explain the role.
  - **Merging:** messages are merged at delivery, through an outbox, so each Outreach wake stays scoped to one exception.
  - **Estimates:** `flag_estimate` now, becoming a send-back once gated ToT exists.
  - **Model:** Haiku 5.5 for the Supervisor.
  - **Parallel:** Outreach fan-out added to this build.
  - **New cut order:** ToT, MCP server, parallel fan-out, message merging.
- Work on branch `module5-supervisor-parallel`.
- Added `tools/outbox.py` (outbox, merge checks, router template merge, delivery with `batch_id`). Schema: `outbox` table (15 tables), `messages.batch_id`, `estimates.flag`. `ReplySimulator` answers a merged message once and files the reply on each exception, has a lock for parallel wakes, and has a new `on_leave_teams` kind.
- Found while planning: a manager who replies "I'm on leave" while Teams shows available could not be escalated before 9:00, against rule 2. Added the Outreach tool `record_manager_leave`, tied to that reply and audited. Email auto-replies also allow early escalation now. The router still uses only deterministic signals.
- `graph.py`: the tick is plan → Outreach fan-out (`Send`, own connection, tools and scope per task) → deliver (merge, deliver, Estimator). Flags `--supervisor`, `--sequential`, `--parallel N`. The report now includes deliveries, model time, Supervisor calls and tokens.
- Added `agents/supervisor.py`: review, merge and queue calls, six tools with code refusals, the router's plan standing on failure, and calls audited with token use.
- Live runs U1 to W2 on Haiku (`docs/LIVE_RUNS.md`):
  - Parallel had the same outcomes as sequential, 2.4 times faster (36 s against 87 s).
  - Merging cut deliveries from 10 to 6.
  - The leave fix worked.
  - The Supervisor changed no outcome on the default morning, at 8–9 calls and about 50–65k input tokens per run.
- Fixed after run W:
  - an agent called a reply to a merged message "ambiguous" and missed the lock, so `check_replies` now marks such replies and the prompt covers them;
  - no-op `escalate_group` calls are refused;
  - a repeated leave record is ignored.
- New tests: `test_outbox.py`, `test_supervisor.py`, plus parallel, merge and Supervisor tests in `test_graph.py`. Full suite 244 passing.
- Open:
  - Supervisor calls run between the parallel steps and roughly double wall time.
  - Its group escalation adds nothing once messages are merged, since the merged reply reaches every exception. It would matter when a manager's exceptions are contacted at different times; covered by tests only.
  - The Outreach prompt changed (leave recording, merged replies), so earlier runs are not exactly comparable.
- Merged as PR #10 (squash, `7e0f2f7`) into `master`.
- **Next:** the Supervisor A/B against the router on the same scenarios (default, `N-02=silent`, stalling, injection, `use_history`, duplicate, `on_leave_teams`), measuring decisions before the lock, wrong resolutions, messages, coordinator interventions, model calls, tokens and latency. Then gated ToT and the MCP server.
- **Behind schedule:** on plan for the 2026-11-17 slot. Still to build: the A/B, gated ToT, the MCP server, then the evaluation tables, report and video.

### 2026-11-18 (Module 5)
- Plan agreed with the user before building: 9 scenarios (the 7 planned, plus `ambiguous`, and a new `late` scenario for the Supervisor's tools), 3 runs per LLM arm, the fixed-reminder baseline as a third arm, and Outreach token tracking. Decision rule fixed before the runs: keep the Supervisor on by default only if it improves decisions before the lock or wrong outcomes in at least one scenario and makes none worse.
- Work on branch `module5-ab-eval`.
- Added the `late` reply kind (`sim/personas.py`): approves 95 minutes after the first message, so with `N-02=silent` both of M-03's exceptions are already with the coordinator when the approval arrives.
- Outreach token use: `agent_act` records each wake's tokens (`token_usage`, shared with the Supervisor) in the `agent_turn` audit entry; `final_report` adds `outreach_tokens`.
- `graph.py`: the CLI's wiring moved into `run_lock_day()`, used by the CLI and the A/B script.
- Added `ab.py`: scenarios with expected statuses at the lock, from the business rules; metrics read from the database at the approval pause (decided, wrong outcomes, wrongful approvals, hand-offs, messages, deliveries, escalations, turns, tokens, Supervisor actions, refused merges, wall time); JSON-lines output; `--summary` writes the tables. `tests/test_ab.py` (18). Full suite 262 passing.
- Ran the A/B while the user was away, as agreed: pass 1 (27 runs) checked first, then passes 2 and 3 (36 runs). 63 runs, no failures, $2.13. Results in `docs/AB_RESULTS.md`; runs AB1–AB63 in `docs/LIVE_RUNS.md`; raw rows in `docs/ab/runs.jsonl`.
  - Router and Supervisor: the same status on every exception in every run (57 decisions, 0 wrong outcomes, 0 wrongful approvals, 15 hand-offs over the 9 scenarios). The Supervisor never used `escalate_group` or `wake_now`; it cost about 17% more tokens and doubled wall time.
  - Agent against baseline: 57 decisions against 25, 0 wrong outcomes against wrong outcomes in all 9 baseline runs, 0 wrongful approvals against 11, and 100 messages against 172.
  - Decision: the router stays the default; the Supervisor stays behind `--supervisor` as the documented experiment.
- Merged as PR #11 (squash, `90448dd`) into `master`.
- Open:
  - The Supervisor may matter at a larger scale (20–30 exceptions, manager facts arriving at different times); not tested.
  - Prompt caching would cut Outreach cost (most input is the same prompt, tools and policy on every turn); not changed, to keep the A/B comparable.
  - "Wrongful approval" relies on the simulator's approval wording.
- **Next:** gated ToT in the Estimator, then the MCP server. Thursday: evaluation tables, report and video.
- **Behind schedule:** the A/B is done on the 2026-11-18 slot; gated ToT and the MCP server are still to build today.


### 2026-11-18 (Modules 5 and 6)
- Order revised with the user: policy-search filters and gated ToT first (Module 5), then Module 6 (metrics, reply screen, ablation, LangSmith), with the MCP server only if time allows.
- Policy search: every policy document states `States:`/`Effective:`/`Until:`; `search_policy` and `policy_for_reply` always filter on the employee's state and the simulated date, and audit the filters. Two out-of-scope distractors added; 0 of 19 gold questions put an out-of-scope rule in the top 3 (3 without filters), recall@3 unchanged at 0.84.
- Gated ToT in the Estimator (`agents/tot.py`) with the Supervisor send-back, closing Checkpoint 5.1; live runs X1–X4.
- Merged as PR #12 (squash, `09eb6f6`) into `master`.
- Module 6, on branch `module6-eval`:
  - `metrics.py`: Checkpoint 6.1 metrics from the database after each run, a scripted known-answer coordinator for calibration, JSON lines in `docs/eval/`, `--summary`, `--ablation`.
  - `tools/guardrails.py`: deterministic reply screen in place of Rebuff (decided with the user), and guardrail switches that audit `guardrail_bypassed`; every refusal audited as `tool_refused`.
  - Fix found by the evaluation: a reply to a merged message is matched to the message it answers by id, not only time (a same-tick reminder had made the screen flag a real approval).
  - 32 live runs (EV1–EV32, $1.27): all targets met; nothing got through in the ablation; Brier 0.373, with the standard-hours confidence too low on this seed (`docs/EVAL_RESULTS.md`).
  - LangSmith tracing checked live with the user's key (LS1–LS2); fixed the morning trace ending as an error at the approval pause.
  - Full suite 321 passing.
- Merged as PR #13 (squash, `01c9337`) into `master`.
- Open:
  - The ablation shows the guardrails as backstops only; adversarial scenarios that make the model attempt refused moves were not built.
  - Confidence placeholders (standard hours 0.30) left unchanged; the report should say so.
  - Two labelled injections pass the screen by design (no instruction, bulk or secrecy words).
- **Next:** final report and video (Thursday), then the MCP server if time allows.
- **Behind schedule:** on plan; the build is done except the MCP server, which is optional.

### 2026-11-19 (Module 4 MCP server, LangSmith)
- LangSmith tracing checked live with the user's Personal Access Token (runs LS1–LS2): traces arrive with run names, tags and metadata, and their token counts match the audit log. Fixed the morning trace ending as an error (`GeneratorExit`) at the approval pause. Included in PR #13.
- Why the MCP server was optional, discussed with the user: it changes no agent behaviour or metric, but Checkpoint 4.1 named it, so a minimal read-only version was built. Plan agreed before building.
- Work on branch `module4-mcp-server`.
- Added `mcp_server.py`. It uses the official `mcp` SDK v2, where FastMCP is now `MCPServer`, and runs over stdio.
  - 7 read-only tools reuse the agents' code. There is no write tool, decided with the user: an MCP client is another model, so the Outreach guardrails and the approval gate stay out of its reach.
  - Every call is audited as actor `mcp`; refusals as `tool_refused`.
  - Tool returns are typed `dict[str, Any]` so clients get structured content.
- `tests/test_mcp_server.py` (9) runs through an in-memory MCP session. Full suite 330 passing.
- Run MCP1: the server was smoke-checked over stdio after a baseline morning, then connected in the user's Claude Desktop. That install reads its config from `AppData\Local\Claude-Data\…`, not `AppData\Roaming\Claude`. Connection guide in `docs/MCP_SERVER.md`.
- Merged as PR #14 (squash, `075b32f`) into `master`. All modules are now built.
- Reviewed what was left with the user: no more building. The open items (confidence tuning, adversarial guardrail scenarios, prompt caching, a larger-scale Supervisor test, a reranker, CI) go in the report as limitations and future work.
- Wrote `README.md`: the problem, how the graph and agents work, design principles, headline results against the baseline, setup, running and evaluation commands, the MCP server, tests, layout and a documentation index. Its commands were checked against the code and the live database. Committed directly to `master` (`0cfc3ae`) and pushed.
- Discussed sharing the private repo with graders: add them as collaborators (write access on a personal repo), make it public (`.env` was never committed), or upload a zip. Not decided.
- Open:
  - The MCP server hasn't been exercised by a model in Claude Desktop beyond listing its tools; the user is testing it while recording the demo.
  - The README's clone line still says `<this repo>`; fill it in once the sharing option is chosen.
- **Next:** Module 7, in a new session: record the demo (agent run in the VS Code terminal, then questions in Claude Desktop over MCP), the final report and the video, and share the repo with the graders.
- **Behind schedule:** on plan; the build is complete, and the report and video are due today (Thu 2026-11-19).

### 2026-11-24 (Module 7)
- README clone step now uses the GitHub URL (`https://github.com/amathad/timesheet-exception-agent.git`), closing the 2026-11-19 open item. The `<path to this repo>` placeholders in `docs/MCP_SERVER.md` stay, since they stand for each reader's local clone. Committed directly to `master` (`b8ce68b`) and pushed.
- Done by the user outside this session, by the Mon 2026-11-23 deadline:
  - the repo was made public for the graders;
  - the MCP server was tested from Claude Desktop against lock-day runs, with the coordinator's questions as prompts;
  - the final report was submitted, with a document linking the presentation video and the elevator pitch, both uploaded to YouTube. The two links are in the README's Videos section.
- No merges; no code changes.
- Open: none.
- **Next:** nothing left to build; the final session is Wed 2026-11-25.
- **Behind schedule:** no; the complete project was submitted by the course deadline.
