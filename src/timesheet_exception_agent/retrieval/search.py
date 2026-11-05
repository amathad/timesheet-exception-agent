"""Hybrid search over policy chunks and past cases: pgvector similarity plus full-text search.

Each search ranks the corpus two ways, by cosine similarity of embeddings (pgvector HNSW)
and by keyword match (PostgreSQL full-text, OR of the query's terms), then merges the two
rankings with reciprocal rank fusion (RRF). Embeddings catch paraphrases ("on vacation" for
"on leave"); keywords catch exact terms the small embedding model can blur ("REG", "9:00",
"retro"). mode="vector" or mode="keyword" runs one ranking alone, for evaluation.

Results carry their source (document and section, or case id) so answers can cite them.
Retrieved text is reference material: business rules are still enforced in code.
"""

import re
from dataclasses import dataclass

from timesheet_exception_agent.retrieval.embeddings import Embedder, to_pgvector

MODES = ("hybrid", "vector", "keyword")
# How a policy source is written in search results and cited in status reasons.
CITATION = re.compile(r"\[([a-z_]+) > ([^\]]+)\]")
RRF_K = 60  # standard RRF constant; damps the weight of the very top ranks
POOL = 20  # candidates taken from each ranking before fusion


class RetrievalError(Exception):
    pass


def citation(doc: str, section: str) -> str:
    return f"[{doc} > {section}]"


@dataclass(frozen=True)
class Corpus:
    table: str
    columns: tuple[str, ...]
    filters: tuple[str, ...]  # columns a caller may filter on with equality


POLICIES = Corpus(
    "policy_chunks", ("id", "doc", "title", "section", "body"), filters=("doc",)
)
CASES = Corpus(
    "case_memory",
    (
        "id",
        "period_end",
        "employee_id",
        "manager_id",
        "exception_type",
        "situation",
        "resolution",
        "outcome",
        "hours_entered",
        "retro",
    ),
    filters=("exception_type", "outcome", "manager_id", "employee_id"),
)


def rrf(rankings: list[list], k: int = RRF_K) -> list[tuple[object, float]]:
    """Reciprocal rank fusion: score(id) = sum over rankings of 1 / (k + rank)."""
    scores: dict = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))


def _where(corpus: Corpus, model: str, filters: dict) -> tuple[str, list]:
    unknown = set(filters) - set(corpus.filters)
    if unknown:
        raise RetrievalError(
            f"cannot filter {corpus.table} on {sorted(unknown)}; allowed: {list(corpus.filters)}"
        )
    clauses, params = ["model = %s"], [model]
    for col, value in sorted(filters.items()):
        clauses.append(f"{col} = %s")
        params.append(value)
    return " AND ".join(clauses), params


def vector_ranking(
    conn,
    corpus: Corpus,
    query_vec: list[float],
    model: str,
    filters: dict,
    pool: int = POOL,
) -> list[tuple[object, float]]:
    """(id, cosine similarity), most similar first."""
    where, params = _where(corpus, model, filters)
    vec = to_pgvector(query_vec)
    rows = conn.execute(
        f"SELECT id, 1 - (embedding <=> %s::vector) FROM {corpus.table} WHERE {where}"
        " ORDER BY embedding <=> %s::vector LIMIT %s",
        [vec, *params, vec, pool],
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def keyword_ranking(
    conn, corpus: Corpus, text: str, model: str, filters: dict, pool: int = POOL
) -> list[object]:
    """Ids matching any of the query's terms, best ts_rank_cd first."""
    where, params = _where(corpus, model, filters)
    # plainto_tsquery ANDs every term, which a full question rarely matches; OR them instead.
    rows = conn.execute(
        "WITH q AS (SELECT replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')"
        "           ::tsquery AS q)"
        f" SELECT id FROM {corpus.table}, q WHERE {where} AND tsv @@ q.q"
        " ORDER BY ts_rank_cd(tsv, q.q) DESC, id LIMIT %s",
        [text, *params, pool],
    ).fetchall()
    return [r[0] for r in rows]


def search(
    conn,
    embedder: Embedder,
    corpus: Corpus,
    query: str,
    k: int = 3,
    mode: str = "hybrid",
    **filters,
) -> list[dict]:
    """Top-k rows of the corpus for the query, each with its fused score."""
    if mode not in MODES:
        raise RetrievalError(f"mode must be one of {MODES}")
    query = query.strip()
    if not query:
        raise RetrievalError("query is empty")
    require_corpus(conn, corpus, embedder.name)

    similarity: dict = {}
    rankings = []
    if mode in ("hybrid", "vector"):
        vec_rank = vector_ranking(
            conn, corpus, embedder.embed_query(query), embedder.name, filters
        )
        similarity = dict(vec_rank)
        rankings.append([i for i, _ in vec_rank])
    if mode in ("hybrid", "keyword"):
        rankings.append(keyword_ranking(conn, corpus, query, embedder.name, filters))

    top = rrf(rankings)[:k]
    if not top:
        return []
    ids = [i for i, _ in top]
    rows = conn.execute(
        f"SELECT {', '.join(corpus.columns)} FROM {corpus.table} WHERE id = ANY(%s)",
        (ids,),
    ).fetchall()
    by_id = {r[0]: dict(zip(corpus.columns, r)) for r in rows}
    results = []
    for item, score in top:
        row = by_id[item]
        row["score"] = round(score, 5)
        row["similarity"] = round(similarity[item], 4) if item in similarity else None
        results.append(row)
    return results


def search_policies(
    conn, embedder: Embedder, query: str, k: int = 3, **kw
) -> list[dict]:
    return search(conn, embedder, POLICIES, query, k, **kw)


def search_cases(conn, embedder: Embedder, query: str, k: int = 3, **kw) -> list[dict]:
    return search(conn, embedder, CASES, query, k, **kw)


def require_corpus(conn, corpus: Corpus, model: str) -> None:
    n = conn.execute(
        f"SELECT count(*) FROM {corpus.table} WHERE model = %s", (model,)
    ).fetchone()[0]
    if n == 0:
        raise RetrievalError(
            f"{corpus.table} has no rows embedded with {model}; run"
            " `uv run python -m timesheet_exception_agent.retrieval.ingest`"
        )


if __name__ == "__main__":
    import sys

    from timesheet_exception_agent.db.connection import get_conn
    from timesheet_exception_agent.retrieval.embeddings import FastEmbedder

    sys.stdout.reconfigure(encoding="utf-8")
    question = " ".join(sys.argv[1:]) or "What if the manager is on vacation?"
    embedder = FastEmbedder()
    with get_conn() as conn:
        print(f"Q: {question}\n\nPolicies:")
        for r in search_policies(conn, embedder, question):
            print(f"  [{r['doc']} > {r['section']}] score {r['score']}")
        print("\nSimilar cases:")
        for r in search_cases(conn, embedder, question):
            print(f"  [{r['id']}] {r['outcome']}: {r['situation']}")
