"""Retrieval: lexical always, semantic when configured, tenant-scoped either way."""

import uuid

from sutr.documentation import embeddings, retrieval
from sutr.documentation.embeddings import EmbeddingProvider, EmbeddingResult
from sutr.models.chunk_embedding import ChunkEmbedding
from sutr.models.document import Document
from sutr.models.document_chunk import DocumentChunk
from sutr.models.org import Org


def _document(session, org_id, filename="policy.md") -> Document:
    document = Document(
        org_id=org_id, filename=filename, kind="markdown", sha256=uuid.uuid4().hex, content=b"x"
    )
    session.add(document)
    session.commit()
    return document


def _chunk(session, document, text, position=0, section="Refunds") -> DocumentChunk:
    chunk = DocumentChunk(
        org_id=document.org_id,
        document_id=document.id,
        position=position,
        section_path=section,
        text=text,
        end_offset=len(text),
    )
    session.add(chunk)
    session.commit()
    return chunk


async def test_lexical_search_ranks_the_relevant_chunk_first(session, test_org):
    document = _document(session, test_org.id)
    _chunk(session, document, "Refunds are allowed only within 30 days of purchase.", 0)
    _chunk(session, document, "The office is closed on public holidays.", 1)

    result = await retrieval.search(session, org_id=test_org.id, query="refund window days")
    assert result.hits
    assert "Refunds are allowed" in result.hits[0].text
    assert result.hits[0].matched_terms


async def test_search_reports_that_it_is_lexical_when_no_embeddings_exist(session, test_org):
    """It must not present BM25 results under a name implying more. A caller
    that believes it got semantic search will trust the ranking differently."""
    document = _document(session, test_org.id)
    _chunk(session, document, "Refunds are allowed within 30 days.")

    result = await retrieval.search(session, org_id=test_org.id, query="refunds")
    assert result.mode == "lexical"
    assert result.semantic_available is False
    assert "NOT_CONFIGURED" in result.semantic_reason
    assert result.as_dict()["semantic"]["available"] is False


async def test_a_query_from_one_org_never_returns_another_orgs_chunk(session, test_org):
    """The negative multi-tenancy test (build prompt §61). Isolation is in the
    WHERE clause, not in a filter applied to results afterwards."""
    other = Org(id=uuid.uuid4(), name="Other Org")
    session.add(other)
    session.commit()

    mine = _document(session, test_org.id, "mine.md")
    theirs = _document(session, other.id, "theirs.md")
    _chunk(session, mine, "Refunds are allowed within 30 days for my tenant.")
    _chunk(session, theirs, "Refunds are allowed within 90 days for the other tenant.")

    result = await retrieval.search(session, org_id=test_org.id, query="refunds allowed days")
    assert result.hits
    assert all(hit.document_id == mine.id for hit in result.hits)
    assert not any("other tenant" in hit.text for hit in result.hits)

    theirs_result = await retrieval.search(session, org_id=other.id, query="refunds allowed days")
    assert all(hit.document_id == theirs.id for hit in theirs_result.hits)


async def test_search_can_be_scoped_to_one_document(session, test_org):
    first = _document(session, test_org.id, "first.md")
    second = _document(session, test_org.id, "second.md")
    _chunk(session, first, "Refunds are processed weekly.")
    _chunk(session, second, "Refunds are processed daily.")

    result = await retrieval.search(
        session, org_id=test_org.id, query="refunds processed", document_id=second.id
    )
    assert [hit.document_id for hit in result.hits] == [second.id]


async def test_stopwords_alone_match_nothing(session, test_org):
    document = _document(session, test_org.id)
    _chunk(session, document, "Refunds are allowed within 30 days.")
    result = await retrieval.search(session, org_id=test_org.id, query="the and is of")
    assert result.hits == []


class _StubProvider(EmbeddingProvider):
    """A deterministic stand-in, used only to exercise the hybrid path.

    Not a substitute for a real model — the production default deliberately
    has none (see embeddings.NOT_CONFIGURED). This one exists so the fusion
    code is tested, and it lives in the test file where it cannot be mistaken
    for a shipped provider.
    """

    id = "stub"
    model = "stub-3"
    dimensions = 3

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    def available(self):
        return True, None

    async def embed(self, texts):
        return EmbeddingResult(
            vectors=[self._vectors.get(text, [0.0, 0.0, 1.0]) for text in texts],
            model=self.model,
            dimensions=self.dimensions,
        )


async def test_hybrid_search_fuses_both_rankings_when_a_provider_is_configured(session, test_org):
    document = _document(session, test_org.id)
    lexical_only = _chunk(session, document, "Reimbursement paperwork must be filed.", 0)
    semantic_only = _chunk(session, document, "Money is returned to the buyer.", 1)

    # The semantic-only chunk shares no query terms, so lexical search cannot
    # find it; only the vector leg can.
    embeddings.set_provider(_StubProvider({"reimbursement": [1.0, 0.0, 0.0]}))
    vector, norm = embeddings.pack([1.0, 0.0, 0.0])
    session.add(
        ChunkEmbedding(
            org_id=test_org.id,
            document_id=document.id,
            chunk_id=semantic_only.id,
            model="stub-3",
            dimensions=3,
            vector=vector,
            norm=norm,
        )
    )
    session.commit()

    result = await retrieval.search(session, org_id=test_org.id, query="reimbursement")
    assert result.mode == "hybrid"
    found = {hit.chunk_id for hit in result.hits}
    assert lexical_only.id in found  # matched lexically
    assert semantic_only.id in found  # matched only by vector


async def test_a_vector_of_the_wrong_dimension_is_skipped_not_averaged(session, test_org):
    """Two models' vectors in one table produce confident nonsense if compared.
    Skipping is right; a similarity between incomparable vectors is not."""
    document = _document(session, test_org.id)
    chunk = _chunk(session, document, "Refunds are allowed.")
    embeddings.set_provider(_StubProvider({}))
    vector, norm = embeddings.pack([1.0, 0.0])
    session.add(
        ChunkEmbedding(
            org_id=test_org.id,
            document_id=document.id,
            chunk_id=chunk.id,
            model="stub-3",
            dimensions=2,  # disagrees with the provider's 3
            vector=vector,
            norm=norm,
        )
    )
    session.commit()

    result = await retrieval.search(session, org_id=test_org.id, query="refunds")
    assert result.mode == "hybrid"
    assert all(hit.semantic_rank is None for hit in result.hits)


def test_bm25_scores_a_rare_term_above_a_common_one():
    chunks = [
        DocumentChunk(text="refunds refunds refunds chargeback", end_offset=1),
        DocumentChunk(text="refunds are mentioned here once", end_offset=1),
        DocumentChunk(text="refunds appear in this one too", end_offset=1),
    ]
    ranked = retrieval.bm25("chargeback", chunks)
    assert len(ranked) == 1
    assert "chargeback" in ranked[0][0].text
