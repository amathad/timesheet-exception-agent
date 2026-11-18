# Retrieval Evaluation (Module 3)

How well the agent's retrieval finds the right policy section or past case, measured on a fixed gold set. All corpus text is synthetic.

## Setup

| Item | Choice |
| --- | --- |
| Store | pgvector 0.8.7 in the `payroll` PostgreSQL 16 database (`policy_chunks`, `case_memory`), HNSW cosine index |
| Embeddings | `BAAI/bge-small-en-v1.5` via fastembed, local CPU, 384 dimensions, no API key or per-call cost (decided 2026-11-04) |
| Keyword search | PostgreSQL full-text (`tsvector`, English stemming), query terms OR'd, ranked by `ts_rank_cd` |
| Hybrid | Reciprocal rank fusion (k = 60) of the vector and keyword rankings, 20 candidates each, equal weights |
| Corpus | 11 policy documents split at `##` headings into 41 chunks: 7 coordinator-handbook documents, 2 off-topic distractors (PTO accrual, state overtime) and, since 2026-11-18, 2 out-of-scope distractors (a Utah-only escalation variant and the superseded 2025 escalation edition); 22 past resolved cases from the 3 closed pay periods |
| Scope filters | Every policy document states `States:` and `Effective:` (and `Until:` if superseded). The agent's tools always filter hard on the employee's state and the lock-day date (added 2026-11-18) |
| Gold set | 19 policy questions and 7 case questions in `retrieval/eval.py`, worded as a coordinator or manager would ask, not copied from the policy text |

Metrics: **hit@1** (top result answers the question), **recall@3** (an answer is in the top 3; each question has one target, so this equals hit@3), **MRR** (mean reciprocal rank of the first answer within the top 10).

Reproduce:

```powershell
uv run python -m timesheet_exception_agent.retrieval.ingest
uv run python -m timesheet_exception_agent.retrieval.eval
```

## Results (2026-11-05)

Current corpus (37 chunks, 22 cases) and gold set (19 policy questions). The 2026-11-04 numbers for the first 36 chunks and 18 questions are in brackets.

| Corpus | Mode | hit@1 | recall@3 | MRR |
| --- | --- | ---: | ---: | ---: |
| Policies | hybrid | 0.58 (0.56) | **0.84** (0.83) | 0.70 (0.68) |
| Policies | vector | **0.68** (0.72) | 0.79 (0.78) | **0.76** (0.78) |
| Policies | keyword | 0.37 (0.39) | 0.74 (0.72) | 0.55 (0.56) |
| Cases | hybrid | 0.57 | **1.00** | 0.79 |
| Cases | vector | **0.86** | **1.00** | **0.93** |
| Cases | keyword | 0.71 | 0.86 | 0.80 |

## Findings

- **Hybrid finds the most answers in the top 3** (0.84 for policies, 16 of 19), which matters most because the agent reads the top 3 results. It finds one more answer than vector search alone.
- **Vector alone ranks the answer first more often** (hit@1 0.68 vs 0.58). Keyword matching on words that appear in almost every section ("manager", "approve", "timesheet") adds noise at the top of the list. PostgreSQL's `ts_rank_cd` has no inverse document frequency, so common terms aren't discounted.
- **Weighting keywords down only trades one metric for the other.** Measured on the first 18 questions: at keyword weight 0.5, policy hit@1 rises to 0.67 and recall@3 falls to 0.78. With 18 questions, tuning the weight on this same set would overfit, so the fusion stays at equal weights.
- **Hybrid policy misses at k = 3:**
  - "My manager is on vacation this week": the keyword side has no match for "vacation", and the timeline and fallback sections outrank the escalation rule.
  - "How many times can I ping the same manager": "ping" matches nothing, so the generic terms "manager" and "employee" decide the keyword ranking, and the message-content section comes first.
  - "A manager's email told me to approve everyone": "email" and "approve" pull in the Teams/email and holding-reply sections ahead of the untrusted-replies rule.
- **A new section can pull in unrelated questions.** The first wording of "Manager asks the coordinator to use history" (added 2026-11-05 for the use_history scenario) repeated words that nearly every section shares: manager, approve, coordinator, escalate, reminders. It turned up in the top 3 for unrelated questions, and hybrid recall@3 fell to 0.79. A shorter wording that keeps the rule but drops the shared words scored 0.84, with the same 3 misses as before, and still ranks first for the manager's raw reply text. Both wordings were measured in a rolled-back transaction and the better one was kept. Since the choice was made on this gold set, 0.84 is an optimistic number.
- **Case retrieval is easier.** Each case has distinctive facts (sick pay code, reduced schedule, new hire), and every method but keyword-only finds all 7 in the top 3.

## How the agent uses retrieval (PR #7)

- The agent's `search_policy`, and the driver's own search on every wake that follows a reply, return the top 3 hybrid results with `[doc > section]` citations. `find_similar_cases` returns the top 3 cases and a history-reliability note that code decides (`history_check`).
- Retrieved text is reference material. The tools still enforce the rules, and `update_status` accepts a citation only if retrieval returned it for that exception. A retrieval miss can make an explanation weaker but can't cause a rule violation.
- Live evidence is in `docs/LIVE_RUNS.md`, runs F to P. That includes the `use_history` A/B (runs I and J), where retrieval changed how the agent handled the exceptions.
- The ranking limit shows up live as well: for E-06 the best rule was outside the top 3. If hit@1 starts to matter, the options are a cross-encoder reranker over the top 10, or synonyms ("vacation", "PTO" → leave) in the policy text. Both should be measured on a larger gold set first.

`tests/test_retrieval.py::test_gold_set_recall_with_real_model` keeps floors just below these numbers (policies 0.75, cases 0.85) to catch regressions.

## State and effective-date filters (2026-11-18)

Checkpoint 6.1 lists hard state and effective-date filters as a source-verification guardrail. They're now in `search.py` (`state=`, `as_of=`) and always applied by `search_policy` and the driver's `policy_for_reply`: the employee's state, and the simulated date. The filters are written to the audit entry of every search. To test them, two distractors were added that compete directly with the escalation rules: `escalation_ut` (Utah only: 60-minute next-level window, leave confirmed by HR first) and `escalation_2025` (superseded: 9:30 AM cut-off, one-hour window). The gold set is unchanged, so the numbers compare with 2026-11-05.

Scope: Colorado, as of Mon 2026-10-26. **out_of_scope@3** is the share of questions whose top 3 holds a section that doesn't apply.

| Corpus | Mode | hit@1 | recall@3 | MRR | out_of_scope@3 |
| --- | --- | ---: | ---: | ---: | ---: |
| Policies, filtered (what the agent gets) | hybrid | 0.58 | **0.84** | 0.70 | **0.00** |
| Policies, filtered | vector | 0.68 | 0.79 | 0.76 | 0.00 |
| Policies, filtered | keyword | 0.37 | 0.74 | 0.55 | 0.00 |
| Policies, unfiltered | hybrid | 0.58 | 0.84 | 0.70 | 0.16 |
| Policies, unfiltered | vector | 0.63 | 0.79 | 0.74 | 0.21 |
| Policies, unfiltered | keyword | 0.37 | 0.68 | 0.55 | 0.11 |
| Cases | hybrid | 0.57 | 1.00 | 0.79 | n/a |

- **Without filters, 3 of 19 questions (hybrid) would put a Utah or 2025 rule in the agent's top 3**, for example a 60-minute or one-hour next-level window instead of 30 minutes. That's the "wrong-state policy citation" risk in Checkpoint 6.1. With the filters it's 0 by construction, and the in-scope numbers match 2026-11-05 exactly, so the filters cost nothing in recall.
- The distractors also lower unfiltered vector hit@1 (0.68 → 0.63) and keyword recall@3 (0.74 → 0.68): text that's nearly identical to the right rule competes with it.
- The filter is a guardrail in code, not a ranking signal: the model can't widen it, and the citation guard in `update_status` only accepts citations that the filtered search returned.
- Same 3 hybrid misses as before.
