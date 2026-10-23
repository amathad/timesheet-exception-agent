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
