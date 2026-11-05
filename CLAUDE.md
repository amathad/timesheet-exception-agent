# Timesheet Exception Agent

CMU Agentic AI capstone. Full context, rules, design decisions, and build status:

@docs/PROJECT_CONTEXT.md

## Working conventions
- Windows machine: give terminal commands in PowerShell only.
- Python 3.12 managed with uv: use 'uv add' for dependencies and 'uv run' to execute.
- PostgreSQL runs in Docker Compose (container payroll-pg, database payroll); connection string in .env as DATABASE_URL.
- All data is synthetic. Never add real names, employers, or payroll records.
- Business rules (deadlines, timers, escalation, hour calculations) go in deterministic code, not prompts.
- Update the Build status checklist in docs/PROJECT_CONTEXT.md when a step is finished.
- At the end of each session, add a dated entry to the Session log in docs/PROJECT_CONTEXT.md, shaped like the earlier ones: the work in order, merges (PR number, squash hash), open items, then **Next:** and **Behind schedule:** last.
- After changing retrieval/policies/*.md or retrieval/cases.py, re-run 'uv run python -m timesheet_exception_agent.retrieval.ingest' and then 'retrieval.eval', and record the new numbers in docs/RETRIEVAL_EVAL.md.
- Tests must never overwrite the real retrieval corpus: use HashEmbedder inside a transaction that is rolled back, and don't run the OutreachDriver (it commits) in retrieval tests.
- Live runs reseed the database and need the corpus ingested first. Record each run in docs/LIVE_RUNS.md, and check results in the database (exceptions, messages, audit_log), not in the agent's printed summaries.
