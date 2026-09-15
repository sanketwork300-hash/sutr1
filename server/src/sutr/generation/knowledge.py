"""Documentation knowledge, gathered for a build.

LLD §3.6 defines the generator's input as *IR + documentation knowledge +
template*. Sutr had the first and the third; this is the second. What it does
is read the rules, vocabulary and workflows the documentation service extracted
for a provider's API, attach the ones that plausibly concern each tool, and
hand the result to the packager, which writes it into `tools.json`.

Why it belongs in the artifact at all: a generated server is often run
somewhere the platform cannot reach, by an agent that will never call Sutr's
retrieval API. A rule that only exists in the control plane is a rule that
agent will not follow. Shipping it with the tools is what makes it operative.

Two properties are load-bearing.

**Every entry keeps its citation** (build prompt §16). A rule arriving in a
generated package still names the document, the location and the sentence it
came from, so an operator reading the package can check it — and so can the
agent, if it is asked to explain why it refused.

**The association is a match, not a claim.** A rule is attached to a tool when
their significant tokens overlap; nothing is inferred, nothing is generated,
and a rule that matches nothing is still shipped at the top level rather than
being dropped. The attachment is a convenience for a reader, not evidence that
the rule governs that operation.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.documentation.linking import tokenize, tool_tokens
from sutr.models.business_rule import BusinessRule
from sutr.models.doc_workflow import DocWorkflow, GlossaryTerm
from sutr.models.document import Document

# Ceilings. A generated package is downloaded, read and shipped in a container;
# a provider with a thousand extracted rules should not turn a 60 KB package
# into a megabyte of prose. The counts are recorded so a truncation is visible
# rather than silent.
MAX_RULES = 200
MAX_TERMS = 200
MAX_WORKFLOWS = 50

# Same threshold as step→operation linking, for the same reason: one shared
# token is noise in a single-domain API.
MIN_TOKEN_OVERLAP = 2


@dataclass
class Knowledge:
    """What the documentation service knows about one API, ready to ship."""

    rules: list[dict[str, Any]] = field(default_factory=list)
    terms: list[dict[str, Any]] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    by_tool: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    truncated: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.rules or self.terms or self.workflows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rules": self.rules,
            "terms": self.terms,
            "workflows": self.workflows,
            "by_tool": self.by_tool,
            "truncated": self.truncated,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "rules": len(self.rules),
            "terms": len(self.terms),
            "workflows": len(self.workflows),
            "tools_annotated": len(self.by_tool),
            "truncated": self.truncated,
        }

    def content_hash(self) -> str:
        """Identity of the knowledge folded into a build.

        Empty knowledge hashes to the empty string rather than to the hash of
        an empty structure, so "this build had no documentation" is legible on
        the artifact record instead of looking like a hash nobody recognises.
        """
        if self.empty:
            return ""
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _citation(row: Any) -> dict[str, Any]:
    return {
        "document": getattr(row, "source_document", ""),
        "location": getattr(row, "source_location", ""),
        "text": getattr(row, "source_text", ""),
        "page": getattr(row, "page", None),
    }


def _rule_entry(rule: BusinessRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "type": rule.rule_type,
        "condition": rule.condition,
        "action": rule.action,
        "confidence": round(rule.confidence, 3),
        "extractor": rule.extractor_version,
        "source": _citation(rule),
    }


def _term_entry(term: GlossaryTerm) -> dict[str, Any]:
    return {
        "id": str(term.id),
        "term": term.term,
        "definition": term.definition,
        "aliases": json.loads(term.aliases_json or "[]"),
        "confidence": round(term.confidence, 3),
        "source": _citation(term),
    }


def _workflow_entry(workflow: DocWorkflow) -> dict[str, Any]:
    return {
        "id": str(workflow.id),
        "name": workflow.name,
        "description": workflow.description,
        "steps": json.loads(workflow.nodes_json or "[]"),
        "edges": json.loads(workflow.edges_json or "[]"),
        "operations": json.loads(workflow.operation_refs_json or "[]"),
        "tools": json.loads(workflow.tool_refs_json or "[]"),
        "source": _citation(workflow),
    }


def _document_ids(session: Session, org_id: uuid.UUID, project_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        session.exec(
            select(Document.id).where(Document.org_id == org_id, Document.project_id == project_id)
        ).all()
    )


def collect(
    session: Session,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
    tools: list[Any],
) -> Knowledge:
    """Gather this project's documentation knowledge, scoped to one tenant.

    Every query filters on `org_id` as well as on the document set, so a
    project id that belonged to another tenant would return nothing rather than
    that tenant's rules.
    """
    knowledge = Knowledge()
    if project_id is None:
        return knowledge
    document_ids = _document_ids(session, org_id, project_id)
    if not document_ids:
        return knowledge

    rules = session.exec(
        select(BusinessRule)
        .where(BusinessRule.org_id == org_id, col(BusinessRule.document_id).in_(document_ids))
        .order_by(desc(BusinessRule.confidence), col(BusinessRule.id))
    ).all()
    terms = session.exec(
        select(GlossaryTerm)
        .where(GlossaryTerm.org_id == org_id, col(GlossaryTerm.document_id).in_(document_ids))
        .order_by(col(GlossaryTerm.term), col(GlossaryTerm.id))
    ).all()
    workflows = session.exec(
        select(DocWorkflow)
        .where(DocWorkflow.org_id == org_id, col(DocWorkflow.document_id).in_(document_ids))
        .order_by(desc(DocWorkflow.confidence), col(DocWorkflow.id))
    ).all()

    if len(rules) > MAX_RULES:
        knowledge.truncated["rules"] = len(rules) - MAX_RULES
    if len(terms) > MAX_TERMS:
        knowledge.truncated["terms"] = len(terms) - MAX_TERMS
    if len(workflows) > MAX_WORKFLOWS:
        knowledge.truncated["workflows"] = len(workflows) - MAX_WORKFLOWS

    kept_rules = list(rules)[:MAX_RULES]
    kept_terms = list(terms)[:MAX_TERMS]
    kept_workflows = list(workflows)[:MAX_WORKFLOWS]

    knowledge.rules = [_rule_entry(rule) for rule in kept_rules]
    knowledge.terms = [_term_entry(term) for term in kept_terms]
    knowledge.workflows = [_workflow_entry(workflow) for workflow in kept_workflows]
    knowledge.by_tool = _attach(kept_rules, kept_terms, tools)
    return knowledge


def _attach(
    rules: list[BusinessRule], terms: list[GlossaryTerm], tools: list[Any]
) -> dict[str, dict[str, list[str]]]:
    """Which rules and terms concern which tool, by token overlap."""
    attached: dict[str, dict[str, list[str]]] = {}
    for tool in sorted(tools, key=lambda item: getattr(item, "name", "")):
        name = getattr(tool, "name", "")
        signature = tool_tokens(tool)
        if not signature:
            continue
        rule_ids = [
            str(rule.id)
            for rule in rules
            if len(tokenize(f"{rule.condition} {rule.action} {rule.source_text}") & signature)
            >= MIN_TOKEN_OVERLAP
        ]
        term_ids = [
            str(term.id)
            for term in terms
            if tokenize(term.term) and tokenize(term.term) <= signature
        ]
        if rule_ids or term_ids:
            attached[name] = {"rules": rule_ids, "terms": term_ids}
    return attached
