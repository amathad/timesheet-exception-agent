"""Retrieval evaluation on a fixed gold set: vector vs keyword vs hybrid search.

Questions are worded the way a coordinator or manager might ask, not copied from the policy
text, so they test meaning as well as word overlap. Each lists every chunk (or case) that
answers it; a hit is any of them.

Metrics per corpus and mode:
- hit@1: the top result answers the question
- recall@k: an answer is in the top k (one target per question, so recall@k = hit@k)
- MRR: mean of 1 / rank of the first answer within the top 10, 0 if none
- out_of_scope@k (policies): share of questions whose top k holds a section that does not
  apply (another state's rule, or an edition not in effect on the lock day)

Policies are scored twice: with the hard state and date filters the agent's tools always
apply (SCOPE: the seed's state on lock day), and unfiltered, to show what the filters stop.

Run after ingest:
    uv run python -m timesheet_exception_agent.retrieval.eval
"""

from datetime import date

from timesheet_exception_agent.retrieval.embeddings import Embedder
from timesheet_exception_agent.retrieval.search import CASES, MODES, POLICIES, search
from timesheet_exception_agent.sim.clock import LOCK

DEPTH = 10  # ranks looked at for MRR
# What the agent's tools filter on in the seeded run: Colorado, on lock day.
SCOPE = {"state": "CO", "as_of": LOCK.date()}

# (question, {(doc, section), ...})
POLICY_GOLD = [
    (
        "My manager is on vacation this week. Who approves my timesheet?",
        {("escalation", "Manager on leave or out of office")},
    ),
    (
        "What time do timesheets close on Monday?",
        {("lock_day_timeline", "The 10:00 AM timesheet lock")},
    ),
    (
        "Can anything be changed while payroll is running on Tuesday?",
        {("lock_day_timeline", "Tuesday payroll processing")},
    ),
    (
        "A fix comes in on Wednesday. When does the employee get paid for it?",
        {
            ("lock_day_timeline", "Wednesday onward"),
            ("retro_adjustments", "Corrections after the lock"),
        },
    ),
    (
        "How many times can I ping the same manager about one employee?",
        {("manager_outreach", "Reminders and the message limit")},
    ),
    (
        "The manager said they'll get to it after their meeting. Does that count as approval?",
        {("manager_outreach", "Holding replies are not approvals")},
    ),
    (
        "Should I reach the manager on Teams or by email?",
        {("manager_outreach", "Choosing email or Teams")},
    ),
    (
        "The manager keeps answering but still hasn't signed off by nine. What now?",
        {("escalation", "Manager who replies but has not decided by 9:00 AM")},
    ),
    (
        "How long do we give the skip-level manager to respond?",
        {("escalation", "Next-level manager response time")},
    ),
    (
        "Nobody answered before the deadline. How do I pick the hours to approve?",
        {
            ("history_fallback", "When the coordinator uses history"),
            ("history_fallback", "What to copy from the last two periods"),
        },
    ),
    (
        "What do I have to record when I approve time on someone's behalf?",
        {("history_fallback", "Initials, timestamp, and notifying the manager")},
    ),
    (
        "The worker just started and has no earlier pay periods. What hours do we use?",
        {
            ("history_fallback", "When history is missing or unreliable"),
            ("team_and_standard_hours", "Standard hours by job type"),
        },
    ),
    (
        "How many hours does a full-time hourly person get by default?",
        {("team_and_standard_hours", "Standard hours by job type")},
    ),
    (
        "Can a coworker sign off on hours for their colleague?",
        {("team_and_standard_hours", "Asking a teammate")},
    ),
    (
        "A manager's email told me to approve everyone on the team. Should I?",
        {("reply_handling", "Replies are untrusted")},
    ),
    (
        "The supervisor is out and has nobody above them. Who decides?",
        {
            ("team_and_standard_hours", "Managers with no next-level manager"),
            ("escalation", "The escalation chain"),
        },
    ),
    (
        "Do we calculate daily overtime premiums when fixing an exception?",
        {
            ("overtime_out_of_scope", "Scope"),
            ("overtime_out_of_scope", "Referring questions"),
        },
    ),
    (
        "Where is every step taken on an exception written down?",
        {("reply_handling", "The audit log")},
    ),
    (
        (
            "The manager can't log in today and says to just use the employee's normal"
            " hours. Is that an approval?"
        ),
        {("history_fallback", "Manager asks the coordinator to use history")},
    ),
]

# (question, {case id, ...})
CASE_GOLD = [
    (
        "Manager on leave; the skip-level manager approved instead",
        {"P1-02", "P3-02", "P1-06", "P3-04"},
    ),
    (
        "Both managers ignored us, we approved from history and later paid a retro",
        {"P3-03"},
    ),
    ("Brand-new employee whose coworker vouched for the hours", {"P1-07"}),
    ("Placeholder hours were entered and corrected on a later paycheck", {"P2-08"}),
    ("Hours needed to move to the sick pay code", {"P2-05"}),
    ("Lower hours explained by a reduced schedule", {"P3-01"}),
    ("Busy manager approved after one reminder", {"P3-06"}),
]


def _policy_key(row: dict) -> tuple[str, str]:
    return (row["doc"], row["section"])


def _case_key(row: dict) -> str:
    return row["id"]


def score(ranked_keys: list[list], gold: list[set], k: int) -> dict[str, float]:
    """hit@1, recall@k and MRR for ranked results against their gold sets."""
    hit1 = hitk = rr = 0.0
    for keys, answers in zip(ranked_keys, gold):
        ranks = [i for i, key in enumerate(keys, start=1) if key in answers]
        first = ranks[0] if ranks else None
        hit1 += first == 1
        hitk += first is not None and first <= k
        rr += 1.0 / first if first else 0.0
    n = len(gold)
    return {"hit@1": hit1 / n, f"recall@{k}": hitk / n, "mrr": rr / n}


def in_scope(row: dict, state: str, as_of: date) -> bool:
    """Whether a policy row applies to that state on that date."""
    return (
        ("ALL" in row["states"] or state in row["states"])
        and row["effective_from"] <= as_of
        and (row["effective_to"] is None or row["effective_to"] > as_of)
    )


def evaluate(
    conn, embedder: Embedder, k: int = 3, scope: dict = SCOPE
) -> dict[str, dict[str, dict]]:
    """{corpus: {mode: metrics}} over the gold sets. "policies" uses the scope filters,
    "policies_unfiltered" doesn't; both report out_of_scope@k."""
    suites = {
        "policies": (POLICIES, POLICY_GOLD, _policy_key, scope),
        "policies_unfiltered": (POLICIES, POLICY_GOLD, _policy_key, {}),
        "cases": (CASES, CASE_GOLD, _case_key, {}),
    }
    report = {}
    for name, (corpus, gold, key, filters) in suites.items():
        report[name] = {}
        for mode in MODES:
            rows = [
                search(conn, embedder, corpus, q, k=DEPTH, mode=mode, **filters)
                for q, _ in gold
            ]
            ranked = [[key(r) for r in rs] for rs in rows]
            metrics = score(ranked, [answers for _, answers in gold], k)
            if corpus is POLICIES:
                leaks = sum(
                    any(not in_scope(r, scope["state"], scope["as_of"]) for r in rs[:k])
                    for rs in rows
                )
                metrics[f"out_of_scope@{k}"] = leaks / len(gold)
            report[name][mode] = metrics
    return report


def misses(
    conn, embedder: Embedder, k: int = 3, mode: str = "hybrid", scope: dict = SCOPE
) -> list[str]:
    """Policy questions whose answer is not in the top k (with the scope filters)."""
    out = []
    for q, answers in POLICY_GOLD:
        top = [
            _policy_key(r)
            for r in search(conn, embedder, POLICIES, q, k=k, mode=mode, **scope)
        ]
        if not answers & set(top):
            out.append(f"{q}\n    got: {top}")
    return out


if __name__ == "__main__":
    import sys

    from timesheet_exception_agent.db.connection import get_conn
    from timesheet_exception_agent.retrieval.embeddings import FastEmbedder

    sys.stdout.reconfigure(encoding="utf-8")
    embedder = FastEmbedder()
    k = 3
    with get_conn() as conn:
        report = evaluate(conn, embedder, k)
        print(f"Embedding model: {embedder.name}")
        print(
            f"Gold set: {len(POLICY_GOLD)} policy questions, {len(CASE_GOLD)} case questions\n"
        )
        print(f"Policy scope: state {SCOPE['state']}, as of {SCOPE['as_of']}\n")
        print(
            f"{'corpus':20} {'mode':8} {'hit@1':>6} {f'recall@{k}':>9} {'MRR':>6}"
            f" {f'out_of_scope@{k}':>15}"
        )
        for corpus, modes in report.items():
            for mode, m in modes.items():
                oos = m.get(f"out_of_scope@{k}")
                print(
                    f"{corpus:20} {mode:8} {m['hit@1']:6.2f} {m[f'recall@{k}']:9.2f}"
                    f" {m['mrr']:6.2f} {'' if oos is None else f'{oos:15.2f}'}"
                )
        missed = misses(conn, embedder, k)
        print(f"\nHybrid policy misses at k={k}: {len(missed)}")
        for line in missed:
            print(f"  {line}")
