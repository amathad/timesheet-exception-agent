"""Text embedders for retrieval.

FastEmbedder runs BAAI/bge-small-en-v1.5 locally through fastembed (ONNX, CPU): no API key,
no per-call cost, and the same vectors on every run. The model (about 70 MB) downloads on
first use into fastembed's cache.

HashEmbedder is a deterministic stand-in for tests: a hashed bag of words, so texts that
share words land close together. It needs no model download.

Both produce 384-dimensional unit vectors, matching the vector(384) columns in schema.sql.
"""

import hashlib
import math
import re
from typing import Protocol

DIM = 384


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    name = "BAAI/bge-small-en-v1.5"

    def __init__(self) -> None:
        self._model = None  # loaded on first use; loading takes a few seconds

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self.name)
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._load().embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # query_embed adds the instruction prefix bge models expect on the query side.
        return next(iter(self._load().query_embed([text]))).tolist()


class HashEmbedder:
    name = "hash-test"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    @staticmethod
    def _embed(text: str) -> list[float]:
        vec = [0.0] * DIM
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.md5(word.encode()).digest()
            vec[int.from_bytes(digest[:4], "little") % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def to_pgvector(vec: list[float]) -> str:
    """pgvector's text format, for a %s::vector parameter."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
