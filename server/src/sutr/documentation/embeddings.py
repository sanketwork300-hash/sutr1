"""Embeddings, and being honest about not having a model.

Build prompt §19 wants Qdrant with a pgvector fallback, and *"embedding
generation must be asynchronous"* — a document must not block on a model call.

There is no embedding model configured in the default install, and this is the
one place where a plausible-looking substitute would do real damage. A hashing
or TF-IDF vector *is* a vector: it has a dimension, it supports cosine
similarity, and it returns ranked results. It is not a semantic embedding, and
a search built on it would look like semantic search while behaving like
lexical search with extra steps. Users would draw conclusions from rankings
that mean something other than what they think.

So the default is **no embeddings**, said plainly, and retrieval falls back to
lexical search over chunks — which genuinely works and is honest about what it
is. When a provider is configured, vectors are generated asynchronously and
stored; the storage, the similarity search and the async job all exist and are
tested. Only the model call is missing, and it is missing visibly.
"""

import array
import asyncio
import math
import uuid
from dataclasses import dataclass

from sqlmodel import Session, col, select

from sutr.models.chunk_embedding import ChunkEmbedding
from sutr.models.document_chunk import DocumentChunk

NOT_CONFIGURED = (
    "NOT_CONFIGURED: no embedding provider is configured, so no vectors are generated and "
    "semantic search is unavailable. Documentation retrieval falls back to lexical search "
    "over chunks, which works and is not the same thing. A hash-based stand-in is "
    "deliberately not used: it would rank results while looking semantic, which is worse "
    "than not ranking them."
)


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    dimensions: int


class EmbeddingProvider:
    """Turns text into vectors."""

    id: str = ""
    model: str = ""
    dimensions: int = 0

    def available(self) -> tuple[bool, str | None]:
        return False, NOT_CONFIGURED

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        raise NotImplementedError


class UnconfiguredProvider(EmbeddingProvider):
    """The default: no model, and it says so."""

    id = "none"
    model = ""
    dimensions = 0

    def available(self) -> tuple[bool, str | None]:
        return False, NOT_CONFIGURED

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        raise RuntimeError(NOT_CONFIGURED)


_provider: EmbeddingProvider = UnconfiguredProvider()


def get_provider() -> EmbeddingProvider:
    return _provider


def set_provider(provider: EmbeddingProvider) -> None:
    """Install a provider. Used by configuration and by tests."""
    global _provider
    _provider = provider


# ── Storage ──────────────────────────────────────────────────────────────────


def pack(vector: list[float]) -> tuple[bytes, float]:
    """Store a vector as float32 bytes, with its norm precomputed.

    Precomputing the norm turns cosine similarity into a dot product at query
    time, which is the difference between a search that scales and one that
    recomputes a square root per candidate.
    """
    packed = array.array("f", vector)
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return packed.tobytes(), norm


def unpack(data: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(data)
    return list(values)


def cosine(left: list[float], left_norm: float, right: list[float], right_norm: float) -> float:
    if len(left) != len(right):
        # Vectors from different models are not comparable, and a similarity
        # score between them would be a number with no meaning.
        raise ValueError("Cannot compare vectors of different dimensions.")
    dot = sum(a * b for a, b in zip(left, right))
    return dot / ((left_norm or 1.0) * (right_norm or 1.0))


async def embed_document(session: Session, *, org_id: uuid.UUID, document_id: uuid.UUID) -> dict:
    """Generate and store vectors for a document's chunks.

    Returns a summary. When no provider is configured this is a no-op that
    reports why, rather than an error: a document is fully processed without
    embeddings, it is just not semantically searchable.
    """
    provider = get_provider()
    usable, reason = provider.available()
    if not usable:
        return {"generated": 0, "skipped": True, "unavailable_reason": reason, "model": None}

    chunks = list(
        session.exec(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(col(DocumentChunk.position))
        ).all()
    )
    if not chunks:
        return {
            "generated": 0,
            "skipped": True,
            "unavailable_reason": "The document has no chunks.",
            "model": None,
        }

    existing = {
        row.chunk_id
        for row in session.exec(
            select(ChunkEmbedding)
            .where(ChunkEmbedding.document_id == document_id)
            .where(ChunkEmbedding.model == provider.model)
        ).all()
    }
    pending = [chunk for chunk in chunks if chunk.id not in existing]
    if not pending:
        return {"generated": 0, "skipped": False, "model": provider.model}

    result = await provider.embed([chunk.text for chunk in pending])
    for chunk, vector in zip(pending, result.vectors):
        data, norm = pack(vector)
        session.add(
            ChunkEmbedding(
                org_id=org_id,
                document_id=document_id,
                chunk_id=chunk.id,
                model=result.model,
                dimensions=result.dimensions,
                vector=data,
                norm=norm,
            )
        )
    return {"generated": len(pending), "skipped": False, "model": result.model}


async def embed_document_async(org_id: uuid.UUID, document_id: uuid.UUID) -> dict:
    """Run embedding generation off the request path.

    Build prompt §19: *"Embedding generation must be asynchronous."* A document
    is processed and usable before its vectors exist; retrieval degrades to
    lexical until they do.
    """
    from sutr import db

    def _run() -> dict:
        with Session(db.engine) as session:
            summary = asyncio.run(embed_document(session, org_id=org_id, document_id=document_id))
            session.commit()
            return summary

    return await asyncio.to_thread(_run)


def describe() -> dict:
    provider = get_provider()
    usable, reason = provider.available()
    return {
        "provider": provider.id,
        "model": provider.model or None,
        "dimensions": provider.dimensions or None,
        "available": usable,
        "unavailable_reason": reason,
    }
