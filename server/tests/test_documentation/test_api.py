"""The /v1/documentation surface, including what one tenant must not see."""

import uuid

import pytest
from sqlmodel import select

from sutr.models.business_rule import BusinessRule
from sutr.models.document import Document
from sutr.models.document_chunk import DocumentChunk
from sutr.models.document_job import DocumentJob
from sutr.models.org import Org

from .conftest import POLICY


async def _upload(client, text=POLICY, filename="policy.md"):
    response = await client.post(
        "/v1/documentation/jobs", json={"content": text, "filename": filename}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def test_creating_a_job_returns_the_lld_job_id_and_the_processed_document(client):
    data = await _upload(client)
    assert uuid.UUID(data["job_id"])
    assert data["job"]["status"] == "completed"
    assert data["document"]["document_type"] == "policy"
    assert data["document"]["status"] in ("processed", "partial")


async def test_a_job_without_a_uri_or_content_is_rejected(client):
    response = await client.post("/v1/documentation/jobs", json={})
    assert response.status_code == 400
    assert "document_uri" in response.json()["error"]["message"]


async def test_a_document_uri_pointing_at_the_local_network_is_refused(client):
    """A documentation URI is user-supplied input aimed at our own network; it
    goes through the same SSRF screen as every other outbound fetch."""
    response = await client.post(
        "/v1/documentation/jobs", json={"document_uri": "http://127.0.0.1:8000/secrets"}
    )
    assert response.status_code == 400
    assert "refused" in response.json()["error"]["message"].lower()


async def test_a_file_upload_takes_the_same_path(client):
    response = await client.post(
        "/v1/documentation/documents",
        files={"file": ("policy.md", POLICY.encode(), "text/markdown")},
    )
    assert response.status_code == 201
    assert response.json()["data"]["document"]["kind"] == "markdown"


async def test_rules_are_returned_with_their_citations(client):
    await _upload(client)
    response = await client.get("/v1/documentation/rules")
    assert response.status_code == 200
    rules = response.json()["data"]
    assert rules
    for rule in rules:
        assert rule["citation"]["text"]
        assert rule["citation"]["location"]
        assert rule["extractor_version"]


async def test_workflows_and_glossary_are_exposed(client):
    await _upload(client)
    workflows = (await client.get("/v1/documentation/workflows")).json()["data"]
    glossary = (await client.get("/v1/documentation/glossary")).json()["data"]
    assert any(w["nodes"] for w in workflows)
    assert any(term["term"] == "Chargeback" for term in glossary)


async def test_the_graph_is_returned_for_a_document(client):
    data = await _upload(client)
    response = await client.get(f"/v1/documentation/graph?document_id={data['document']['id']}")
    body = response.json()["data"]
    assert body["nodes"]
    assert body["edges"]


async def test_search_says_which_mode_it_ran_in(client):
    await _upload(client)
    response = await client.get("/v1/documentation/search?q=refund within 30 days")
    body = response.json()["data"]
    assert body["mode"] == "lexical"
    assert body["semantic"]["available"] is False
    assert body["hits"]
    assert body["hits"][0]["document_name"] == "policy.md"


async def test_capabilities_report_what_is_missing_and_why(client):
    body = (await client.get("/v1/documentation/capabilities")).json()["data"]
    assert body["embeddings"]["available"] is False
    assert "NOT_CONFIGURED" in body["embeddings"]["unavailable_reason"]
    assert body["storage"]["object_store"]["available"] is False
    assert {entry["kind"] for entry in body["parsers"]} >= {"text", "markdown", "pdf", "ocr"}
    assert any(entry["id"] == "rule_based" and entry["available"] for entry in body["extractors"])


async def test_a_job_can_be_fetched_with_its_stage_record(client):
    data = await _upload(client)
    response = await client.get(f"/v1/documentation/jobs/{data['job_id']}")
    job = response.json()["data"]
    assert [stage["name"] for stage in job["stages"]] == [
        "parse",
        "chunk",
        "extract",
        "graph",
        "embed",
    ]


async def test_reprocessing_runs_a_fresh_job(client):
    data = await _upload(client)
    document_id = data["document"]["id"]
    response = await client.post(f"/v1/documentation/documents/{document_id}/reprocess")
    assert response.status_code == 200
    assert response.json()["data"]["job"]["id"] != data["job_id"]


async def test_deleting_a_document_removes_everything_derived_from_it(client, session):
    """A rule whose source document is gone is a claim with no citation."""
    data = await _upload(client)
    document_id = uuid.UUID(data["document"]["id"])

    assert session.exec(select(BusinessRule)).all()
    response = await client.delete(f"/v1/documentation/documents/{document_id}")
    assert response.status_code == 204

    assert session.exec(select(Document)).all() == []
    assert session.exec(select(BusinessRule)).all() == []
    assert session.exec(select(DocumentChunk)).all() == []
    assert session.exec(select(DocumentJob)).all() == []


async def test_documents_can_be_filtered_by_type_and_status(client):
    await _upload(client)
    await _upload(client, "# Notes\n\nThe river was calm.\n", "notes.md")

    policies = (await client.get("/v1/documentation/documents?document_type=policy")).json()["data"]
    assert len(policies) == 1
    assert policies[0]["filename"] == "policy.md"


# ── Cross-tenant negative tests (build prompt §61) ───────────────────────────


@pytest.fixture(name="other_org_document")
def other_org_document_fixture(session):
    """A document belonging to a tenant the client is not a member of."""
    other = Org(id=uuid.uuid4(), name="Other Org")
    session.add(other)
    session.commit()
    document = Document(
        org_id=other.id, filename="theirs.md", kind="markdown", sha256="deadbeef", content=b"x"
    )
    session.add(document)
    session.commit()
    session.add(
        BusinessRule(
            org_id=other.id,
            document_id=document.id,
            rule_type="limits",
            action="Their refunds are allowed within 90 days",
            source_document="theirs.md",
            source_text="Their refunds are allowed within 90 days.",
        )
    )
    session.add(
        DocumentChunk(
            org_id=other.id,
            document_id=document.id,
            text="Their refunds are allowed within 90 days.",
            end_offset=41,
        )
    )
    session.commit()
    return document


async def test_another_orgs_document_is_not_listed(client, other_org_document):
    body = (await client.get("/v1/documentation/documents")).json()["data"]
    assert body == []


async def test_another_orgs_document_reads_as_not_found(client, other_org_document):
    """Not 403: distinguishing "forbidden" from "absent" turns the endpoint
    into an oracle for which documents another tenant holds."""
    response = await client.get(f"/v1/documentation/documents/{other_org_document.id}")
    assert response.status_code == 404


async def test_another_orgs_document_cannot_be_deleted(client, session, other_org_document):
    response = await client.delete(f"/v1/documentation/documents/{other_org_document.id}")
    assert response.status_code == 404
    assert session.get(Document, other_org_document.id) is not None


async def test_another_orgs_document_cannot_be_reprocessed(client, other_org_document):
    response = await client.post(f"/v1/documentation/documents/{other_org_document.id}/reprocess")
    assert response.status_code == 404


async def test_another_orgs_rules_and_chunks_are_never_returned(client, other_org_document):
    rules = (await client.get("/v1/documentation/rules")).json()["data"]
    assert rules == []
    chunks = await client.get(f"/v1/documentation/documents/{other_org_document.id}/chunks")
    assert chunks.status_code == 404


async def test_search_never_crosses_the_tenant_boundary(client, other_org_document):
    body = (await client.get("/v1/documentation/search?q=refunds allowed 90 days")).json()["data"]
    assert body["hits"] == []
