"""The pipeline: stages, checkpoints, and the difference between partial and failed."""

import json
import uuid

from sqlmodel import select

from sutr.documentation import embeddings, ingestion, jobs
from sutr.documentation.parsing import ParserUnavailableError
from sutr.models.business_rule import BusinessRule
from sutr.models.doc_workflow import DocWorkflow, GlossaryTerm
from sutr.models.document import STATUS_FAILED, STATUS_PARTIAL, STATUS_PROCESSED
from sutr.models.document_chunk import DocumentChunk
from sutr.models.document_job import (
    STAGE_EMBED,
    STAGE_EXTRACT,
    STAGE_GRAPH,
    STAGE_PARSE,
    STAGES,
    STATUS_COMPLETED,
)
from sutr.models.document_job import STATUS_FAILED as JOB_FAILED
from sutr.models.knowledge_graph import (
    ENTITY_BUSINESS_RULE,
    ENTITY_DOCUMENT,
    KnowledgeEdge,
    KnowledgeNode,
)
from sutr.models.outbox_event import OutboxEvent

from .conftest import POLICY


async def _run(session, org_id, text=POLICY, filename="policy.md", **kwargs):
    result = ingestion.ingest_bytes(session, org_id=org_id, data=text.encode(), filename=filename)
    session.commit()
    job = await jobs.run_job(session, result.job, **kwargs)
    return result.document, job


async def test_a_document_runs_every_stage_and_ends_processed(session, test_org):
    document, job = await _run(session, test_org.id)

    assert job.status == STATUS_COMPLETED
    assert json.loads(job.completed_stages_json) == list(STAGES)
    assert job.error_code is None
    session.refresh(document)
    # No embedding provider is configured, so the run is honest about being
    # incomplete rather than claiming a clean processed state.
    assert document.status == STATUS_PARTIAL
    embeddings_lost = next(
        d for d in json.loads(document.degradations_json) if d["code"] == "embeddings_unavailable"
    )
    # The degradation carries the provider's own reason, not a generic
    # fallback: a user reading "no embeddings were generated" learns nothing
    # they could act on.
    assert "NOT_CONFIGURED" in embeddings_lost["message"]


async def test_each_stage_records_what_it_produced(session, test_org):
    _, job = await _run(session, test_org.id)
    stages = {entry["name"]: entry for entry in json.loads(job.stage_results_json)}

    assert stages[STAGE_PARSE]["status"] == "ok"
    assert stages[STAGE_PARSE]["detail"]["elements"] > 0
    assert stages[STAGE_PARSE]["detail"]["document_type"] == "policy"
    assert stages[STAGE_EXTRACT]["detail"]["rules"] > 0
    assert stages[STAGE_GRAPH]["detail"]["nodes"] > 0
    # No provider, so the embed stage is skipped rather than reported "ok".
    assert stages[STAGE_EMBED]["status"] == "skipped"


async def test_extraction_is_persisted_with_its_citations(session, test_org):
    document, job = await _run(session, test_org.id)

    rules = session.exec(select(BusinessRule).where(BusinessRule.document_id == document.id)).all()
    assert rules
    for rule in rules:
        assert rule.source_document == "policy.md"
        assert rule.source_text
        assert rule.source_location
        assert rule.extractor_version == job.extractor_version
        assert rule.org_id == test_org.id

    assert session.exec(select(DocWorkflow).where(DocWorkflow.document_id == document.id)).all()
    assert session.exec(select(GlossaryTerm).where(GlossaryTerm.document_id == document.id)).all()


async def test_the_graph_links_facts_back_to_their_document(session, test_org):
    document, _ = await _run(session, test_org.id)
    nodes = session.exec(select(KnowledgeNode)).all()
    edges = session.exec(select(KnowledgeEdge)).all()
    assert any(node.entity_type == ENTITY_DOCUMENT for node in nodes)
    assert any(node.entity_type == ENTITY_BUSINESS_RULE for node in nodes)
    assert edges
    assert all(node.org_id == test_org.id for node in nodes)
    assert all(node.document_id in (None, document.id) for node in nodes)


async def test_reprocessing_replaces_facts_rather_than_duplicating_them(session, test_org):
    document, _ = await _run(session, test_org.id)
    first = len(session.exec(select(BusinessRule)).all())

    from sutr.models.document_job import DocumentJob

    again = DocumentJob(org_id=test_org.id, document_id=document.id)
    session.add(again)
    session.commit()
    await jobs.run_job(session, again)

    assert len(session.exec(select(BusinessRule)).all()) == first
    assert len(session.exec(select(DocumentChunk)).all()) > 0


async def test_a_completed_stage_is_not_run_again(session, test_org, monkeypatch):
    """The LLD §3.5 checkpoint. Re-running a job must resume, not restart —
    re-parsing a 300-page PDF because the graph step failed is absurd."""
    document, job = await _run(session, test_org.id)

    calls = []
    real_extract = jobs.get_extractor

    def counting_extractor(extractor_id=None):
        extractor = real_extract(extractor_id)
        original = extractor.extract

        def extract(chunks):
            calls.append(len(chunks))
            return original(chunks)

        extractor.extract = extract
        return extractor

    monkeypatch.setattr(jobs, "get_extractor", counting_extractor)
    await jobs.run_job(session, job)
    assert calls == []  # extract was already checkpointed as complete


async def test_a_graph_failure_degrades_the_run_instead_of_failing_it(
    session, test_org, monkeypatch
):
    """LLD §3.5: graph failure ⇒ continue without graph + warning. The facts
    were still extracted, and throwing them away would be the worse answer."""

    def exploding_build(*args, **kwargs):
        raise RuntimeError("graph backend unreachable")

    monkeypatch.setattr(jobs.graph_module, "build_graph", exploding_build)
    document, job = await _run(session, test_org.id)

    assert job.status == STATUS_COMPLETED
    session.refresh(document)
    assert document.status == STATUS_PARTIAL
    degradations = {d["code"] for d in json.loads(job.degradations_json)}
    assert "graph_unavailable" in degradations
    stages = {entry["name"]: entry for entry in json.loads(job.stage_results_json)}
    assert stages[STAGE_GRAPH]["status"] == "skipped"
    # The rules survived the graph failure.
    assert session.exec(select(BusinessRule).where(BusinessRule.document_id == document.id)).all()


async def test_a_parser_that_is_not_installed_fails_the_job_with_a_reason(
    session, test_org, monkeypatch
):
    """Parsing is the one stage whose failure is terminal: a document nobody
    can read has nothing downstream to do."""

    def unavailable(*args, **kwargs):
        raise ParserUnavailableError("PDF parsing needs pypdf.", install_hint="uv add pypdf")

    monkeypatch.setattr(jobs, "parse_document", unavailable)
    document, job = await _run(session, test_org.id)

    assert job.status == JOB_FAILED
    assert job.error_code == "parser_unavailable"
    assert "pypdf" in job.error_message
    session.refresh(document)
    assert document.status == STATUS_FAILED


async def test_the_event_chain_is_emitted_in_order(session, test_org):
    document, _ = await _run(session, test_org.id)
    types = [
        event.event_type
        for event in session.exec(select(OutboxEvent).order_by(OutboxEvent.created_at)).all()
    ]
    assert types[0] == "documentation.uploaded"
    assert "parsing.completed" in types
    assert "knowledge.extracted" in types
    assert types[-1] == "documentation.processed"
    # No provider, so no embeddings event is claimed.
    assert "embeddings.generated" not in types


async def test_a_failed_job_emits_a_failure_event(session, test_org, monkeypatch):
    def unavailable(*args, **kwargs):
        raise ParserUnavailableError("no parser", install_hint="install one")

    monkeypatch.setattr(jobs, "parse_document", unavailable)
    await _run(session, test_org.id)

    types = [event.event_type for event in session.exec(select(OutboxEvent)).all()]
    assert "documentation.failed" in types
    assert "documentation.processed" not in types


async def test_every_event_is_scoped_to_the_uploading_tenant(session, test_org):
    await _run(session, test_org.id)
    for event in session.exec(select(OutboxEvent)).all():
        assert event.tenant_id == str(test_org.id)


async def test_the_same_bytes_uploaded_twice_produce_one_document(session, test_org):
    first = ingestion.ingest_bytes(
        session, org_id=test_org.id, data=POLICY.encode(), filename="policy.md"
    )
    session.commit()
    second = ingestion.ingest_bytes(
        session, org_id=test_org.id, data=POLICY.encode(), filename="policy-copy.md"
    )
    session.commit()

    assert second.deduplicated is True
    assert second.document.id == first.document.id
    # A new job, though: reprocessing the same document is a legitimate ask.
    assert second.job.id != first.job.id


async def test_identical_documents_in_different_orgs_stay_separate(session, test_org):
    """Deduplication is per tenant. Sharing a row across orgs would leak the
    existence of one tenant's document to another."""
    from sutr.models.org import Org

    other = Org(id=uuid.uuid4(), name="Other Org")
    session.add(other)
    session.commit()

    mine = ingestion.ingest_bytes(session, org_id=test_org.id, data=POLICY.encode())
    theirs = ingestion.ingest_bytes(session, org_id=other.id, data=POLICY.encode())
    session.commit()
    assert mine.document.id != theirs.document.id
    assert theirs.deduplicated is False


async def test_embeddings_are_generated_when_a_provider_is_configured(session, test_org):
    from .test_retrieval import _StubProvider

    embeddings.set_provider(_StubProvider({}))
    document, job = await _run(session, test_org.id)

    session.refresh(document)
    assert document.status == STATUS_PROCESSED  # nothing was lost
    stages = {entry["name"]: entry for entry in json.loads(job.stage_results_json)}
    assert stages[STAGE_EMBED]["status"] == "ok"
    assert stages[STAGE_EMBED]["detail"]["generated"] > 0

    types = [event.event_type for event in session.exec(select(OutboxEvent)).all()]
    assert "embeddings.generated" in types
