"""Load the retrieval corpus into PostgreSQL: policy chunks and past resolved cases.

Policies are split at their ## headings, so each chunk is one rule with its document title
attached. A section longer than MAX_WORDS is split again at paragraph breaks. Ingest is
idempotent: each run replaces the corpus with what is on disk.

The caller owns the transaction; nothing here commits.

Run (downloads the embedding model on first use):
    uv run python -m timesheet_exception_agent.retrieval.ingest
"""

from dataclasses import dataclass
from pathlib import Path

from timesheet_exception_agent.retrieval.cases import CASES, Case
from timesheet_exception_agent.retrieval.embeddings import Embedder, to_pgvector

POLICY_DIR = Path(__file__).resolve().parent / "policies"
MAX_WORDS = 180


@dataclass(frozen=True)
class Chunk:
    doc: str
    title: str
    section: str
    body: str

    def text(self) -> str:
        """What gets embedded: the body with its document and section names for context."""
        return f"{self.title}: {self.section}\n{self.body}"


def chunk_markdown(doc: str, text: str, max_words: int = MAX_WORDS) -> list[Chunk]:
    """Split one policy document into chunks, one per ## section."""
    title = doc
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        elif line.strip():
            raise ValueError(f"{doc}: text before the first ## heading")

    chunks = []
    for heading, lines in sections:
        paragraphs = [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]
        if not paragraphs:
            raise ValueError(f"{doc}: section {heading!r} is empty")
        parts = _pack(paragraphs, max_words)
        for i, body in enumerate(parts, start=1):
            section = heading if len(parts) == 1 else f"{heading} (part {i})"
            chunks.append(Chunk(doc, title, section, body))
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
            "INSERT INTO policy_chunks (doc, title, section, body, model, embedding)"
            " VALUES (%s, %s, %s, %s, %s, %s::vector)",
            (c.doc, c.title, c.section, c.body, embedder.name, to_pgvector(vec)),
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
