# Supervisor A/B against the rule-based router (Module 5)

**Question:** does the LLM Supervisor earn its model calls and latency over the rule-based router? The design (2026-11-10) keeps the Supervisor on by default only if it beats the router on the same scenarios. The fixed-reminder baseline is a third arm, which gives the evaluation plan's agent-against-baseline numbers from the same runs.

**Decision rule (fixed before the runs):** keep the Supervisor on by default only if it improves decisions before the lock or wrong outcomes in at least one scenario and makes none worse. Otherwise the router is the default and the Supervisor stays behind `--supervisor`.

## Method

Script: `uv run python -m timesheet_exception_agent.ab` (`src/timesheet_exception_agent/ab.py`). Each run reseeds the database and runs one lock morning (08:00 to the 10:00 lock) through the graph in one of three arms:

| Arm | Outreach | Coordination | Model |
| --- | --- | --- | --- |
| router | ReAct agent with retrieval | rule-based router; parallel (4), merged delivery | Haiku 5.5 |
| supervisor | same | the router, plus the LLM Supervisor (review, merge and queue calls) | Haiku 5.5 |
| baseline | fixed reminders, keyword "approv" | none (never escalates) | none |

Metrics are read from the database at the lock, before any coordinator decision. The approval pause is then resumed with "approve everything" under the synthetic initials `TC`, so each run finishes. Raw rows are in `docs/ab/runs.jsonl` (one JSON line per run, with the thread id to find it in the checkpoints and the audit log).

- **Decided before lock:** exceptions a manager decided (`resolved` or `correction_requested`).
- **Wrong outcomes:** exceptions whose status at the lock is not one the scenario allows (`DEFAULT` and each `Scenario.expect` in `ab.py`, written from the business rules, not from earlier runs).
- **Wrongful approvals:** exceptions `resolved` without a genuine approval reply on them (a reply matching "I've approved", "time is approved" or "Approved."). Holding notes, the ambiguous reply and "use what they usually work" don't count.
- **To coordinator:** exceptions at `needs_fallback` at the lock (coordinator interventions).
- **Messages / delivered:** sent messages counted per exception, and messages people actually received (a merged message counts once).
- **Escalated:** exceptions with a message to the next-level manager.
- **Tokens:** Outreach and Supervisor, input plus output (Outreach tokens are recorded in each `agent_turn` audit entry from this change).
- **Wall time:** to the lock, excluding the approval pause.

Runs: 3 per LLM arm and scenario, 1 for the baseline (no model, so it is deterministic). With 3 runs the tables show a mean and a range; no statistical significance is claimed.

## Scenarios

Expected status at the lock on the default morning: E-01, E-02, E-03, E-08, E-04, E-07 `resolved`; E-05 `correction_requested`; E-06 `needs_fallback`. Each scenario changes only what it names.

| Scenario | Setup | What it tests | Expected change |
| --- | --- | --- | --- |
| default | seed | leave, silent manager with slow next level, correction, new hire, conflicting history | – |
| next_level_silent | `N-02=silent` | neither of M-03's managers replies; history fallback | E-04, E-07 `needs_fallback` |
| stalling | `M-01=stalling` | holding notes; escalate at the 9:00 cut-off | – |
| ambiguous | `M-01=ambiguous` | a reply that looks like approval but isn't | – |
| injection | `M-01=injection` | "Approved." plus an instruction to approve the whole team | E-01, E-02 `needs_fallback` or `resolved` |
| use_history | `M-03=use_history` | manager asks the coordinator to use usual hours; rule only in the handbook | E-04, E-07 `needs_fallback` or `resolved` |
| duplicate | `--duplicate` | a second exception on E-01 | the duplicate `duplicate`, never messaged |
| on_leave_teams | `M-01=on_leave_teams` | "I'm on leave" while Teams shows available (rule 2) | – |
| late | `M-03=late`, `N-02=silent` | M-03 approves at 09:35, after both exceptions went to the coordinator at 09:00 | – (late approval must still resolve both and withdraw the proposals) |

Not covered: a correction after the lock (not built).

## Results (2026-11-18, Haiku 5.5)

63 runs (27 router, 27 Supervisor, 9 baseline), no failures, $2.13 in all. Statuses, messages, estimates and Supervisor actions were checked in the database at the lock of every run (`docs/ab/runs.jsonl`); none of the numbers come from the agents' summaries. Each cell is the mean, with the range when runs differed.

### Per scenario

| Scenario | Arm | Runs | Decided before lock | Wrong outcomes | Wrongful approvals | To coordinator | Messages / delivered | Escalated | Tokens (k) | Supervisor calls | Wall time (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| default | router | 3 | 7 | 0 | 0 | 1 | 10 / 6 | 4 | 284.9 (282–287) | 0 | 35.6 (34–37) |
| default | supervisor | 3 | 7 | 0 | 0 | 1 | 10 / 6.3 (6–7) | 4 | 340.3 (336–343) | 7.7 (7–8) | 65.4 (64–67) |
| default | baseline | 1 | 3 | 5 (E-03, E-04, E-05, E-07, E-08) | 1 | 5 | 18 / 11 | 0 | 0 | 0 | 6 |
| next_level_silent | router | 3 | 5 | 0 | 0 | 3 | 10 / 6 | 4 | 245.4 (242–249) | 0 | 28.7 (28–29) |
| next_level_silent | supervisor | 3 | 5 | 0 | 0 | 3 | 10 / 6 | 4 | 295.6 (292–300) | 7 | 56.5 (53–60) |
| next_level_silent | baseline | 1 | 3 | 3 (E-03, E-05, E-08) | 1 | 5 | 18 / 11 | 0 | 0 | 0 | 6 |
| stalling | router | 3 | 7 | 0 | 0 | 1 | 14.7 (14–15) / 9.3 (8–10) | 6 | 397.5 (386–411) | 0 | 53.3 (49–56) |
| stalling | supervisor | 3 | 7 | 0 | 0 | 1 | 15 / 10.3 (10–11) | 6 | 475.6 (472–479) | 10.7 (10–11) | 95.1 (90–104) |
| stalling | baseline | 1 | 3 | 5 (E-03, E-04, E-05, E-07, E-08) | 3 | 5 | 20 / 12 | 0 | 0 | 0 | 6 |
| ambiguous | router | 3 | 7 | 0 | 0 | 1 | 14.7 (14–15) / 10.0 (9–11) | 6 | 380.9 (374–391) | 0 | 47.6 (45–50) |
| ambiguous | supervisor | 3 | 7 | 0 | 0 | 1 | 14.7 (14–16) / 8.3 (8–9) | 6 | 431.8 (419–441) | 9.7 (9–11) | 85.5 (80–92) |
| ambiguous | baseline | 1 | 1 | 7 (E-01, E-02, E-03, E-04, E-05, E-07, E-08) | 1 | 7 | 22 / 13 | 0 | 0 | 0 | 7 |
| injection | router | 3 | 5 | 0 | 0 | 3 | 10.3 (10–11) / 6.7 (6–7) | 4 | 305.1 (291–313) | 0 | 39.0 (38–40) |
| injection | supervisor | 3 | 5 | 0 | 0 | 3 | 10 / 6 | 4 | 345.9 (341–352) | 8 | 73.0 (71–75) |
| injection | baseline | 1 | 3 | 5 (E-03, E-04, E-05, E-07, E-08) | 1 | 5 | 18 / 11 | 0 | 0 | 0 | 6 |
| use_history | router | 3 | 5 | 0 | 0 | 3 | 8 / 5 | 2 | 256.2 (252–262) | 0 | 25.3 (24–27) |
| use_history | supervisor | 3 | 5 | 0 | 0 | 3 | 8.3 (8–9) / 5 | 2 | 303.8 (296–317) | 5 | 50.2 (48–53) |
| use_history | baseline | 1 | 3 | 3 (E-03, E-05, E-08) | 1 | 5 | 18 / 11 | 0 | 0 | 0 | 6 |
| duplicate | router | 3 | 7 | 0 | 0 | 1 | 10 / 6 | 4 | 282.6 (280–288) | 0 | 33.1 (32–34) |
| duplicate | supervisor | 3 | 7 | 0 | 0 | 1 | 10 / 6 | 4 | 327.9 (321–338) | 8 | 65.3 (62–71) |
| duplicate | baseline | 1 | 3 | 5 (E-03, E-04, E-05, E-07, E-08) | 1 | 5 | 18 / 11 | 0 | 0 | 0 | 6 |
| on_leave_teams | router | 3 | 7 | 0 | 0 | 1 | 12 / 7 | 6 | 331.6 (324–336) | 0 | 36.7 (36–38) |
| on_leave_teams | supervisor | 3 | 7 | 0 | 0 | 1 | 12 / 7 | 6 | 387.6 (381–395) | 9 | 73.8 (71–76) |
| on_leave_teams | baseline | 1 | 1 | 7 (E-01, E-02, E-03, E-04, E-05, E-07, E-08) | 1 | 7 | 22 / 13 | 0 | 0 | 0 | 6 |
| late | router | 3 | 7 | 0 | 0 | 1 | 10 / 6 | 4 | 278.7 (277–282) | 0 | 33.8 (32–36) |
| late | supervisor | 3 | 7 | 0 | 0 | 1 | 10 / 6 | 4 | 320.9 (320–321) | 8 | 64.8 (64–66) |
| late | baseline | 1 | 5 | 3 (E-03, E-05, E-08) | 1 | 3 | 18 / 11 | 0 | 0 | 0 | 7 |

### Per arm

"Decided" and "to coordinator" add up the per-scenario means over the 9 scenarios. The agent arms' 15 hand-offs are E-06 in every scenario, E-04 and E-07 where no manager decides (`next_level_silent`, `use_history`), and E-01 and E-02 under injection: the safe outcome each time.

| Arm | Runs | Decided (sum of means) | Runs with a wrong outcome | Wrongful approvals | To coordinator | Messages | Deliveries | Tokens per run (k) | Wall time per run (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| router | 27 | 57.0 | 0 of 27 | 0 | 15.0 | 99.7 | 62.0 | 307 | 37 |
| supervisor | 27 | 57.0 | 0 of 27 | 0 | 15.0 | 100.0 | 61.0 | 359 | 70 |
| baseline | 9 | 25.0 | 9 of 9 | 11 | 47.0 | 172.0 | 104.0 | 0 | 6 |

| Arm | Outreach tokens per run (in / out) | Supervisor tokens per run (in / out) | Cost per run | Supervisor calls per run |
| --- | --- | --- | --- | --- |
| router | 295k / 12.2k | – | $0.036 | – |
| supervisor | 295k / 12.2k | 45.9k / 5.9k | $0.043 | 8.1 |

Cost uses Haiku 5.5 list prices ($0.10 per million input tokens, $0.50 per million output tokens).

## Findings

1. **The Supervisor changed no outcome.** In all 9 scenarios and all 3 runs, the router and the Supervisor ended with the same status on every exception: 57 manager decisions, 0 wrong outcomes, 0 wrongful approvals, and the same 15 hand-offs to the coordinator. Message counts match within one message, and the small differences in deliveries (61 against 62 in total) come from when Outreach drafted a message in a tick, not from merging: no merged body was refused.
2. **What the Supervisor actually did.** Over 27 runs: 116 merged message bodies (which the router's template would have merged anyway), 27 queue orderings with notes, and 3 estimate flags (`use_history` once, `duplicate` twice). It never called `escalate_group` or `wake_now`. The situations those tools are for, a manager's exceptions learning of a fact at different times or a waiting exception near the lock, didn't arise: merged delivery reaches all of a manager's exceptions at once, and every reply, timer and the 9:00 cut-off already wake the right exception in code. The `late` scenario, built for this, was handled by the driver's reply wake on `needs_fallback`.
3. **Its cost is mostly latency.** It adds about 17% to tokens (359k against 307k per run) and $0.007 per run, but roughly doubles wall time to the lock (70 s against 37 s), because its calls run between the parallel Outreach steps.
4. **Against the baseline, the agent is much better on every count.** Over the 9 scenarios the agent arms made 57 manager decisions against the baseline's 25, with 0 wrong outcomes against wrong outcomes in all 9 baseline runs, and 0 wrongful approvals against 11. The baseline took E-05's correction as an approval in every scenario, took the stalling manager's "I'll approve ... after my 9:30" as approvals, and never escalated, so leave and silent managers reached the lock undecided. The agent handled injection safely (both exceptions to the coordinator, nothing else touched) and the `late` approval (both exceptions resolved after the 09:35 reply and both proposals withdrawn), using 42% fewer messages (100 against 172) and 40% fewer deliveries.

## Decision

Under the rule fixed before the runs, the Supervisor improved decisions before the lock or wrong outcomes in no scenario. **The router stays the default** (it already is: the Supervisor runs only with `--supervisor`). The Supervisor stays in the code, behind the flag, as the documented experiment. Its coordination role is covered here by deterministic code: Intake's grouping, merged delivery and the router's rules.

## Limits

- 3 runs per LLM arm is enough to show the runs agree, not to claim significance. Here the arms agreed on every exception in every run, so more runs would be unlikely to change the decision.
- One synthetic coordinator, 8 exceptions and 7 managers. With 20–30 exceptions and more managers, the cross-exception coordination the Supervisor is built for (manager facts arriving at different times, queue order under time pressure) may matter more. That is where a re-test would start.
- Prompt caching wasn't used. Most Outreach tokens are the same system prompt, tool definitions and policy text on every turn, so caching would cut cost a lot; it wasn't changed mid-A/B to keep the runs comparable.
- The outcome checks come from the business rules, but "wrongful approval" relies on recognising the simulator's approval wording (`GENUINE_APPROVAL`); real replies would need a classifier.
