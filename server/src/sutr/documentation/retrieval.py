"""Searching a tenant's documentation.

Two legs, fused:

- **Lexical** — BM25 over chunk text, always available. Scored in Python over
  a tenant-scoped candidate set rather than in SQL, because the one thing that
  must not vary by database engine is which documents a tenant can see.
- **Semantic** — cosine similarity over stored embeddings, available only when
  an embedding provider is configured. When it is not, search is lexical and
  *says so* in the response, rather than quietly returning worse results under
  the same name.

Fusion is Reciprocal Rank Fusion: it combines two rankings without needing
their scores to be on the same scale, which BM25 scores and cosine
similarities emphatically are not.

Tenant scoping is not a filter applied at the end. `org_id` is a required
argument, it is in the SQL `WHERE`, and `tests/test_documentation/` asserts
that a query from one org cannot surface another org's chunk.
"""

import math
import uuid
from dataclasses import dataclass, field

from sqlmodel import Session, col, select

from sutr.common import lexical
from sutr.documentation import embeddings as embedding_module
from sutr.models.chunk_embedding import ChunkEmbedding
from sutr.models.document import Document
from sutr.models.document_chunk import DocumentChunk

# The scoring itself lives in `common/lexical.py`: discovery ranks tools with
# the same function, and two copies of BM25 are two copies that disagree the
# first time one is tuned.
K1 = lexical.K1
B = lexical.B
RRF_K = lexical.RRF_K

tokenize = lexical.tokenize


@dataclass
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    section_path: str
    page: int | None
    text: str
    score: float
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    matched_terms: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "document_name": self.document_name,
            "section_path": self.section_path,
            "page": self.page,
            "text": self.text,
            "score": round(self.score, 6),
            "lexical_rank": self.lexical_rank,
            "semantic_rank": self.semantic_rank,
            "matched_terms": self.matched_terms,
        }


@dataclass
class SearchResult:
    hits: list[SearchHit]
    mode: str  # "lexical" | "hybrid"
    semantic_available: bool
    semantic_reason: str | None = None
    candidates: int = 0

    def as_dict(self) -> dict:
        return {
            "hits": [hit.as_dict() for hit in self.hits],
            "mode": self.mode,
            "semantic": {
                "available": self.semantic_available,
                "unavailable_reason": self.semantic_reason,
            },
            "candidates_scored": self.candidates,
        }


def _candidates(
    session: Session,
    org_id: uuid.UUID,
    document_id: uuid.UUID | None,
    limit: int,
) -> list[DocumentChunk]:
    statement = select(DocumentChunk).where(DocumentChunk.org_id == org_id)
    if document_id is not None:
        statement = statement.where(DocumentChunk.document_id == document_id)
    return list(session.exec(statement.limit(limit)).all())


def bm25(query: str, chunks: list[DocumentChunk]) -> list[tuple[DocumentChunk, float, list[str]]]:
    """Rank `chunks` against `query`. Returns non-zero scores only."""
    by_id = {chunk.id: chunk for chunk in chunks}
    ranked = lexical.bm25(query, [(chunk.id, chunk.text) for chunk in chunks])
    return [(by_id[key], score, matched) for key, score, matched in ranked]


async def _semantic(
    session: Session,
    org_id: uuid.UUID,
    query: str,
    document_id: uuid.UUID | None,
    limit: int,
) -> tuple[list[tuple[uuid.UUID, float]], bool, str | None]:
    provider = embedding_module.get_provider()
    usable, reason = provider.available()
    if not usable:
        return [], False, reason

    result = await provider.embed([query])
    if not result.vectors:
        return [], False, "The embedding provider returned no vector for the query."
    query_vector = result.vectors[0]
    query_norm = math.sqrt(sum(value * value for value in query_vector)) or 1.0

    statement = select(ChunkEmbedding).where(
        ChunkEmbedding.org_id == org_id,
        ChunkEmbedding.model == result.model,
    )
    if document_id is not None:
        statement = statement.where(ChunkEmbedding.document_id == document_id)
    rows = list(session.exec(statement.limit(limit)).all())

    ranked: list[tuple[uuid.UUID, float]] = []
    for row in rows:
        # A dimension mismatch means two models' vectors are in the same table
        # under one name. Skipping is right; averaging them is not.
        if row.dimensions != len(query_vector):
            continue
        vector = embedding_module.unpack(row.vector)
        ranked.append(
            (row.chunk_id, embedding_module.cosine(query_vector, query_norm, vector, row.norm))
        )
    ranked.sort(key=lambda entry: entry[1], reverse=True)
    return ranked, True, None


async def search(
    session: Session,
    *,
    org_id: uuid.UUID,
    query: str,
    document_id: uuid.UUID | None = None,
    limit: int = 10,
    candidate_limit: int = 2000,
) -> SearchResult:
    """Search one tenant's chunks. `org_id` is required, and it is the filter."""
    limit = max(1, min(limit, 100))
    chunks = _candidates(session, org_id, document_id, candidate_limit)
    lexical = bm25(query, chunks)

    semantic, semantic_available, semantic_reason = await _semantic(
        session, org_id, query, document_id, candidate_limit
    )

    by_id = {chunk.id: chunk for chunk in chunks}
    matched_terms = {chunk.id: terms for chunk, _, terms in lexical}
    lexical_rank = {chunk.id: index + 1 for index, (chunk, _, _) in enumerate(lexical)}
    semantic_rank = {
        chunk_id: index + 1
        for index, (chunk_id, _) in enumerate(semantic)
        if chunk_id in by_id  # a vector whose chunk was deleted ranks nothing
    }

    fused: dict[uuid.UUID, float] = {}
    for chunk_id, rank in lexical_rank.items():
        fused[chunk_id] = fused.get(chunk_id, 0.0) + 1 / (RRF_K + rank)
    for chunk_id, rank in semantic_rank.items():
        fused[chunk_id] = fused.get(chunk_id, 0.0) + 1 / (RRF_K + rank)

    order = sorted(fused.items(), key=lambda entry: (-entry[1], str(entry[0])))[:limit]
    names = _document_names(session, org_id, {by_id[cid].document_id for cid, _ in order})

    hits = []
    for chunk_id, score in order:
        chunk = by_id[chunk_id]
        hits.append(
            SearchHit(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_name=names.get(chunk.document_id, ""),
                section_path=chunk.section_path,
                page=chunk.page,
                text=chunk.text,
                score=score,
                lexical_rank=lexical_rank.get(chunk_id),
                semantic_rank=semantic_rank.get(chunk_id),
                matched_terms=matched_terms.get(chunk_id, []),
            )
        )

    return SearchResult(
        hits=hits,
        mode="hybrid" if semantic_available else "lexical",
        semantic_available=semantic_available,
        semantic_reason=semantic_reason,
        candidates=len(chunks),
    )


def _document_names(
    session: Session, org_id: uuid.UUID, document_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not document_ids:
        return {}
    rows = session.exec(
        select(Document).where(
            Document.org_id == org_id,
            col(Document.id).in_(document_ids),
        )
    ).all()
    return {row.id: row.filename for row in rows}
