# Project Context: Timesheet Exception Agent

Capstone project for the CMU School of Computer Science program *Agent AI Development for Real World Applications* (Sept–Nov 2026). This file records the problem, the rules the agent must follow, the design decisions made so far, and where the build stands. Update it at the end of each working session.

**Capstone option:** Forecaster
**Status as of 2026-10-22:** project scaffold and database running; no agent code yet.

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
| Model provider | Match whatever the course labs use (to confirm) |

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
- [ ] Load `schema.sql` and confirm 10 tables
- [ ] Verify Python connection (`uv run python -m timesheet_exception_agent.db.connection`)
- [ ] Synthetic data generator: 1 state, 10 employees, 4 managers, 6–8 exceptions, personas
- [ ] Simulated clock and persona-driven reply generator
- [ ] Outreach tools (`get_exception`, `get_presence`, `get_next_level_manager`, `send_message`, `check_replies`, `update_status`, `get_time`)
- [ ] Outreach agent with ReAct, plus the driver loop (Monday 8:00 → 10:00 AM in 10-minute steps)
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

- Confirm the model provider used in the labs
- Confirm exact weekly module deadlines
- Week 3: decide pgvector only, or compare with Chroma for the design write-up
