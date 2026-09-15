"""Documentation knowledge reaching a build, and steps reaching operations."""

import json
import uuid

from sqlmodel import select

from sutr.documentation import linking
from sutr.generation import knowledge as knowledge_module
from sutr.models.business_rule import BusinessRule
from sutr.models.doc_workflow import DocWorkflow, GlossaryTerm
from sutr.models.document import Document
from sutr.models.org import Org

from .conftest import TOOLS


def _document(session, org_id, project_id) -> Document:
    document = Document(
        org_id=org_id,
        project_id=project_id,
        filename="pets.md",
        media_type="text/markdown",
        kind="markdown",
        source_uri="",
        size_bytes=10,
        sha256="a" * 64,
        content=b"",
    )
    session.add(document)
    session.flush()
    return document


def _rule(session, org_id, document, **overrides) -> BusinessRule:
    fields = {
        "org_id": org_id,
        "document_id": document.id,
        "rule_type": "eligibility",
        "condition": "the pet has been vaccinated",
        "action": "create pet is allowed",
        "source_document": "pets.md",
        "source_location": "Eligibility",
        "source_text": "A pet may be created only when it has been vaccinated.",
        "confidence": 0.8,
        "extractor_version": "rules/1",
    }
    fields.update(overrides)
    rule = BusinessRule(**fields)
    session.add(rule)
    session.flush()
    return rule


def _workflow(session, org_id, document, steps) -> DocWorkflow:
    workflow = DocWorkflow(
        org_id=org_id,
        document_id=document.id,
        name="Onboarding",
        nodes_json=json.dumps(
            [{"id": str(i), "label": label, "order": i} for i, label in enumerate(steps)]
        ),
        source_document="pets.md",
        confidence=0.7,
    )
    session.add(workflow)
    session.flush()
    return workflow


def test_a_step_that_names_an_operation_is_linked_to_it(session, test_org):
    project_id = uuid.uuid4()
    document = _document(session, test_org.id, project_id)
    _workflow(session, test_org.id, document, ["Create Pet record", "Notify the owner"])

    report = linking.link_workflows(session, org_id=test_org.id, project_id=project_id, tools=TOOLS)
    assert report.steps == 2
    assert report.linked_steps == 1
    link = report.links[0]
    assert link.tool == "create_pet"
    assert link.operation == "POST /pets"

    workflow = session.exec(select(DocWorkflow)).one()
    assert json.loads(workflow.operation_refs_json) == ["POST /pets"]
    assert json.loads(workflow.tool_refs_json) == ["create_pet"]


def test_a_step_that_matches_nothing_stays_unmatched(session, test_org):
    """Attaching it to the nearest-looking operation would be a wrong link that
    reads exactly like a verified one."""
    project_id = uuid.uuid4()
    document = _document(session, test_org.id, project_id)
    _workflow(session, test_org.id, document, ["Escalate to the compliance desk"])

    report = linking.link_workflows(session, org_id=test_org.id, project_id=project_id, tools=TOOLS)
    assert report.linked_steps == 0
    assert json.loads(session.exec(select(DocWorkflow)).one().operation_refs_json) == []


def test_one_shared_word_is_not_enough_to_link_a_step():
    assert linking.match_step("Review the pets policy", TOOLS) is None


def test_another_tenants_workflows_are_never_linked(session, test_org, test_user):
    other = Org(name="Other", slug="other-org", owner_user_id=test_user.id)
    session.add(other)
    session.flush()
    project_id = uuid.uuid4()
    document = _document(session, other.id, project_id)
    document.org_id = other.id
    session.add(document)
    workflow = _workflow(session, other.id, document, ["Create Pet record"])

    report = linking.link_workflows(session, org_id=test_org.id, project_id=project_id, tools=TOOLS)
    assert report.workflows == 0
    session.refresh(workflow)
    assert json.loads(workflow.operation_refs_json) == []


def test_collected_knowledge_keeps_every_citation(session, test_org):
    project_id = uuid.uuid4()
    document = _document(session, test_org.id, project_id)
    _rule(session, test_org.id, document)
    session.add(
        GlossaryTerm(
            org_id=test_org.id,
            document_id=document.id,
            term="pet",
            definition="An animal in the store's care.",
            source_document="pets.md",
            source_location="Definitions",
            source_text="Pet means an animal in the store's care.",
            confidence=0.6,
        )
    )
    session.flush()

    collected = knowledge_module.collect(
        session, org_id=test_org.id, project_id=project_id, tools=TOOLS
    )
    assert collected.summary()["rules"] == 1
    assert collected.rules[0]["source"]["document"] == "pets.md"
    assert collected.rules[0]["source"]["text"].startswith("A pet may be created")
    assert collected.terms[0]["source"]["location"] == "Definitions"


def test_knowledge_is_attached_to_the_tool_it_mentions(session, test_org):
    project_id = uuid.uuid4()
    document = _document(session, test_org.id, project_id)
    _rule(session, test_org.id, document)
    collected = knowledge_module.collect(
        session, org_id=test_org.id, project_id=project_id, tools=TOOLS
    )
    assert "create_pet" in collected.by_tool
    assert collected.by_tool["create_pet"]["rules"]


def test_a_project_with_no_documents_yields_empty_knowledge_and_no_hash(session, test_org):
    collected = knowledge_module.collect(
        session, org_id=test_org.id, project_id=uuid.uuid4(), tools=TOOLS
    )
    assert collected.empty
    # Empty knowledge hashes to "" so the artifact record can say "none" rather
    # than showing a hash of nothing.
    assert collected.content_hash() == ""


def test_another_tenants_knowledge_is_never_collected(session, test_org, test_user):
    other = Org(name="Other", slug="other-knowledge", owner_user_id=test_user.id)
    session.add(other)
    session.flush()
    project_id = uuid.uuid4()
    document = _document(session, other.id, project_id)
    _rule(session, other.id, document)

    collected = knowledge_module.collect(
        session, org_id=test_org.id, project_id=project_id, tools=TOOLS
    )
    assert collected.empty


def test_knowledge_beyond_the_ceiling_is_truncated_visibly(session, test_org, monkeypatch):
    monkeypatch.setattr(knowledge_module, "MAX_RULES", 2)
    project_id = uuid.uuid4()
    document = _document(session, test_org.id, project_id)
    for index in range(5):
        _rule(session, test_org.id, document, condition=f"condition {index}")

    collected = knowledge_module.collect(
        session, org_id=test_org.id, project_id=project_id, tools=TOOLS
    )
    assert len(collected.rules) == 2
    assert collected.truncated["rules"] == 3
