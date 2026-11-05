import math

import pytest

from timesheet_exception_agent.db.connection import get_conn
from timesheet_exception_agent.retrieval.cases import CASES
from timesheet_exception_agent.retrieval.embeddings import DIM, HashEmbedder
from timesheet_exception_agent.retrieval.eval import (
    CASE_GOLD,
    POLICY_GOLD,
    evaluate,
    score,
)
from timesheet_exception_agent.retrieval.ingest import (
    MAX_WORDS,
    chunk_markdown,
    ingest,
    load_policies,
)
from timesheet_exception_agent.retrieval.search import (
    RetrievalError,
    rrf,
    search_cases,
    search_policies,
)
from timesheet_exception_agent.sim.personas import REPLIES
from timesheet_exception_agent.sim.seed import SCHEMA, STAFF, seed


@pytest.fixture
def conn():
    """Ingests run inside this transaction and are rolled back, so the real corpus stays."""
    with get_conn() as c:
        c.execute(SCHEMA.read_text())
        yield c
        c.rollback()


@pytest.fixture
def embedder():
    return HashEmbedder()


@pytest.fixture
def ingested(conn, embedder):
    ingest(conn, embedder)
    return conn


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# Chunking and corpus files (no database)


def test_chunk_markdown_one_chunk_per_section():
    text = "# Title\n\n## First\nAlpha.\n\n## Second\nBeta one.\n\nBeta two.\n"
    chunks = chunk_markdown("doc", text)
    assert [(c.title, c.section) for c in chunks] == [
        ("Title", "First"),
        ("Title", "Second"),
    ]
    assert chunks[1].body == "Beta one.\n\nBeta two."
    assert chunks[0].text() == "Title: First\nAlpha."


def test_chunk_markdown_splits_long_sections_at_paragraphs():
    para = " ".join(["word"] * 6)
    text = f"# T\n\n## Long\n{para}\n\n{para}\n\n{para}\n"
    chunks = chunk_markdown("doc", text, max_words=12)
    assert [c.section for c in chunks] == ["Long (part 1)", "Long (part 2)"]
    assert chunks[0].body == f"{para}\n\n{para}"


def test_chunk_markdown_rejects_text_outside_sections():
    with pytest.raises(ValueError):
        chunk_markdown("doc", "# T\nstray text\n## S\nbody\n")
    with pytest.raises(ValueError):
        chunk_markdown("doc", "# T\n## Empty\n\n## S\nbody\n")


def test_policy_corpus_loads():
    chunks = load_policies()
    assert len({c.doc for c in chunks}) == 9
    keys = [(c.doc, c.section) for c in chunks]
    assert len(keys) == len(set(keys))
    assert all(len(c.body.split()) <= MAX_WORDS for c in chunks)


def test_cases_match_the_seed():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    staff = {s[0] for s in STAFF}
    for c in CASES:
        # E-9x are earlier new hires who have left, so they are not seeded.
        assert c.employee_id in staff or c.employee_id.startswith("E-9")
    assert {c.outcome for c in CASES} == {
        "manager_approved",
        "next_level_approved",
        "correction",
        "history_fallback",
        "team_confirmed",
        "standard_hours",
    }


def test_gold_labels_point_at_real_chunks_and_cases():
    keys = {(c.doc, c.section) for c in load_policies()}
    for question, answers in POLICY_GOLD:
        assert answers <= keys, question
    case_ids = {c.id for c in CASES}
    for question, answers in CASE_GOLD:
        assert answers <= case_ids, question


# Ranking helpers (no database)


def test_hash_embedder_is_deterministic_unit_length(embedder):
    a = embedder.embed_query("manager on leave")
    assert a == embedder.embed_documents(["manager on leave"])[0]
    assert len(a) == DIM
    assert math.isclose(sum(x * x for x in a), 1.0)


def test_rrf_rewards_items_ranked_well_by_both():
    fused = rrf([["a", "b", "c"], ["c", "a"]], k=60)
    assert [item for item, _ in fused] == ["a", "c", "b"]
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)


def test_score_metrics():
    ranked = [["x", "y"], ["y", "x"], ["z"]]
    gold = [{"x"}, {"x"}, {"x"}]
    m = score(ranked, gold, k=3)
    assert m == {
        "hit@1": pytest.approx(1 / 3),
        "recall@3": pytest.approx(2 / 3),
        "mrr": pytest.approx((1 + 0.5 + 0) / 3),
    }


# Ingest and search (database, hash embedder)


def test_ingest_is_idempotent(conn, embedder):
    first = ingest(conn, embedder)
    second = ingest(conn, embedder)
    assert (
        first
        == second
        == {"policy_chunks": len(load_policies()), "case_memory": len(CASES)}
    )
    assert count(conn, "policy_chunks") == first["policy_chunks"]


def test_reseeding_keeps_the_corpus(ingested):
    seed(ingested)
    assert count(ingested, "policy_chunks") > 0
    assert count(ingested, "case_memory") == len(CASES)


def test_search_returns_sources_and_scores(ingested, embedder):
    results = search_policies(ingested, embedder, "Tuesday payroll processing changes")
    assert len(results) == 3
    top = results[0]
    assert (top["doc"], top["section"]) == (
        "lock_day_timeline",
        "Tuesday payroll processing",
    )
    assert top["score"] > 0 and top["similarity"] is not None
    assert results[0]["score"] >= results[1]["score"] >= results[2]["score"]


def test_keyword_mode_matches_exact_terms(ingested, embedder):
    results = search_policies(ingested, embedder, "retro adjustment", mode="keyword")
    assert results[0]["doc"] == "retro_adjustments"
    assert results[0]["similarity"] is None


def test_case_search_filters_on_columns(ingested, embedder):
    results = search_cases(
        ingested, embedder, "manager did not reply", k=5, exception_type="unusual_hours"
    )
    assert results and all(r["exception_type"] == "unusual_hours" for r in results)
    with pytest.raises(RetrievalError):
        search_cases(ingested, embedder, "anything", body="x")


def test_search_rejects_bad_input(ingested, embedder):
    with pytest.raises(RetrievalError):
        search_policies(ingested, embedder, "   ")
    with pytest.raises(RetrievalError):
        search_policies(ingested, embedder, "lock", mode="semantic")


def test_search_refuses_vectors_from_another_model(ingested):
    class Other(HashEmbedder):
        name = "some-other-model"

    with pytest.raises(RetrievalError, match="run"):
        search_policies(ingested, Other(), "lock time")


# Real embedding model (local fastembed; skipped if the model can't be loaded)


@pytest.fixture(scope="module")
def fastembed_model():
    from timesheet_exception_agent.retrieval.embeddings import FastEmbedder

    model = FastEmbedder()
    try:
        model.embed_query("warm up")
    except Exception as e:  # noqa: BLE001 - offline with no cached model, for example
        pytest.skip(f"fastembed model unavailable: {e}")
    return model


def test_gold_set_recall_with_real_model(conn, fastembed_model):
    ingest(conn, fastembed_model)
    report = evaluate(conn, fastembed_model, k=3)
    # Floors a little under the measured values (docs/RETRIEVAL_EVAL.md) to catch regressions.
    assert report["policies"]["hybrid"]["recall@3"] >= 0.75
    assert report["cases"]["hybrid"]["recall@3"] >= 0.85


def test_real_model_finds_the_use_history_rule_from_the_raw_reply(
    conn, fastembed_model
):
    # The use_history scenario: the rule is only in the handbook, not in the prompt.
    ingest(conn, fastembed_model)
    reply = REPLIES["use_history"][1].format(employee="Gray Nakamura")
    top = [
        (r["doc"], r["section"]) for r in search_policies(conn, fastembed_model, reply)
    ]
    assert ("history_fallback", "Manager asks the coordinator to use history") in top
