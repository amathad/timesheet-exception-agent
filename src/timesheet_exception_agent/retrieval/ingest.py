"""Load the retrieval corpus into PostgreSQL: policy chunks and past resolved cases.

Policies are split at their ## headings, so each chunk is one rule with its document title
attached. A section longer than MAX_WORDS is split again at paragraph breaks. Ingest is
idempotent: each run replaces the corpus with what is on disk.

The caller owns the transaction; nothing here commits.

Run (downloads the embedding model on first use):
    uv run python -m timesheet_exception_agent.retrieval.ingest
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from timesheet_exception_agent.retrieval.cases import CASES, Case
from timesheet_exception_agent.retrieval.embeddings import Embedder, to_pgvector

POLICY_DIR = Path(__file__).resolve().parent / "policies"
MAX_WORDS = 180


ALL_STATES = "ALL"
DEFAULT_EFFECTIVE = date(2000, 1, 1)
META_KEYS = ("States", "Effective", "Until")


@dataclass(frozen=True)
class Chunk:
    doc: str
    title: str
    section: str
    body: str
    # Where and when the document applies (its States/Effective/Until lines).
    states: tuple[str, ...] = (ALL_STATES,)
    effective_from: date = DEFAULT_EFFECTIVE
    effective_to: date | None = None  # exclusive; None = still current

    def text(self) -> str:
        """What gets embedded: the body with its document and section names for context."""
        return f"{self.title}: {self.section}\n{self.body}"


def _parse_meta(doc: str, meta: dict[str, str]) -> dict:
    out: dict = {}
    if "States" in meta:
        states = tuple(s.strip().upper() for s in meta["States"].split(",") if s.strip())
        bad = [s for s in states if s != ALL_STATES and not re.fullmatch(r"[A-Z]{2}", s)]
        if not states or bad:
            raise ValueError(f"{doc}: States must be ALL or two-letter codes, got {meta['States']!r}")
        out["states"] = states
    try:
        if "Effective" in meta:
            out["effective_from"] = date.fromisoformat(meta["Effective"])
        if "Until" in meta:
            out["effective_to"] = date.fromisoformat(meta["Until"])
    except ValueError as e:
        raise ValueError(f"{doc}: bad date ({e})") from e
    if out.get("effective_to") and out["effective_to"] <= out.get(
        "effective_from", DEFAULT_EFFECTIVE
    ):
        raise ValueError(f"{doc}: Until must be after Effective")
    return out


def chunk_markdown(doc: str, text: str, max_words: int = MAX_WORDS) -> list[Chunk]:
    """Split one policy document into chunks, one per ## section.

    Between the # title and the first ## heading, a document may give `States:` (ALL or
    comma-separated two-letter codes), `Effective:` and `Until:` (ISO dates, Until
    exclusive). Every chunk of the document carries them."""
    title = doc
    meta: dict[str, str] = {}
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        elif line.strip():
            key, sep, value = line.partition(":")
            if sep and key.strip() in META_KEYS:
                meta[key.strip()] = value.strip()
            else:
                raise ValueError(f"{doc}: text before the first ## heading")
    scope = _parse_meta(doc, meta)

    chunks = []
    for heading, lines in sections:
        paragraphs = [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]
        if not paragraphs:
            raise ValueError(f"{doc}: section {heading!r} is empty")
        parts = _pack(paragraphs, max_words)
        for i, body in enumerate(parts, start=1):
            section = heading if len(parts) == 1 else f"{heading} (part {i})"
            chunks.append(Chunk(doc, title, section, body, **scope))
    return chunks


def _pack(paragraphs: list[str], max_words: int) -> list[str]:
    """Group paragraphs into parts of at most max_words (a longer paragraph stands alone)."""
    parts, current, words = [], [], 0
    for p in paragraphs:
        n = len(p.split())
        if current and words + n > max_words:
            parts.append("\n\n".join(current))
            current, words = [], 0
        current.append(p)
        words += n
    parts.append("\n\n".join(current))
    return parts


def load_policies(directory: Path = POLICY_DIR) -> list[Chunk]:
    chunks = []
    for path in sorted(directory.glob("*.md")):
        chunks.extend(chunk_markdown(path.stem, path.read_text(encoding="utf-8")))
    return chunks


def ingest_policies(conn, embedder: Embedder, chunks: list[Chunk] | None = None) -> int:
    chunks = load_policies() if chunks is None else chunks
    vectors = embedder.embed_documents([c.text() for c in chunks])
    conn.execute("DELETE FROM policy_chunks")
    for c, vec in zip(chunks, vectors):
        conn.execute(
            "INSERT INTO policy_chunks (doc, title, section, body, states,"
            " effective_from, effective_to, model, embedding)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
            (
                c.doc,
                c.title,
                c.section,
                c.body,
                list(c.states),
                c.effective_from,
                c.effective_to,
                embedder.name,
                to_pgvector(vec),
            ),
        )
    return len(chunks)


def ingest_cases(conn, embedder: Embedder, cases: list[Case] = CASES) -> int:
    vectors = embedder.embed_documents([c.text() for c in cases])
    conn.execute("DELETE FROM case_memory")
    for c, vec in zip(cases, vectors):
        conn.execute(
            "INSERT INTO case_memory (id, period_end, employee_id, manager_id,"
            " exception_type, situation, resolution, outcome, hours_entered, retro,"
            " model, embedding)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
            (
                c.id,
                c.period_end,
                c.employee_id,
                c.manager_id,
                c.exception_type,
                c.situation,
                c.resolution,
                c.outcome,
                c.hours_entered,
                c.retro,
                embedder.name,
                to_pgvector(vec),
            ),
        )
    return len(cases)


def ingest(conn, embedder: Embedder) -> dict[str, int]:
    return {
        "policy_chunks": ingest_policies(conn, embedder),
        "case_memory": ingest_cases(conn, embedder),
    }


if __name__ == "__main__":
    from timesheet_exception_agent.db.connection import get_conn
    from timesheet_exception_agent.retrieval.embeddings import FastEmbedder
    from timesheet_exception_agent.sim.seed import SCHEMA

    embedder = FastEmbedder()
    with get_conn() as conn:
        conn.execute(SCHEMA.read_text())  # creates the retrieval tables if missing
        counts = ingest(conn, embedder)
        conn.commit()
    print(f"Embedding model: {embedder.name}")
    for table, n in counts.items():
        print(f"{table:15} {n}")
