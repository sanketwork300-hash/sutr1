"""The knowledge graph, built from what was extracted.

Build prompt §18 prefers Neo4j and fixes the vocabulary — entities Provider,
Customer, Account, Payment, Refund, API, Tool, Workflow, BusinessRule, Role,
Region, ComplianceRequirement; relationships OWNS, CONTAINS, ELIGIBLE_FOR,
IMPLEMENTS, REQUIRES, MAY_INVOKE, CONTAINS_OPERATION.

It also states the invariant that matters most: **"Graph is enrichment. It must
never replace the canonical IR."** Nothing in the platform reads a tool
definition from here. The graph answers "what is related to what", the IR
answers "what is this API", and confusing the two would make a derived,
heuristic structure authoritative.

The graph is stored in the platform's own database, always. Neo4j is a mirror
for deployments that have one (ADR-015) — a graph that exists only when an
optional service is running is a graph nothing can depend on, and the LLD's own
failure rule is *"graph failure ⇒ continue without graph + warning"*.

Entities come from what was actually extracted, never from guessing: a Refund
node exists because a rule or a workflow mentioned refunds, not because
payments APIs usually have one.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, col, select

from sutr.models.knowledge_graph import (
    ENTITY_BUSINESS_RULE,
    ENTITY_COMPLIANCE,
    ENTITY_DOCUMENT,
    ENTITY_REGION,
    ENTITY_ROLE,
    ENTITY_TERM,
    ENTITY_TOOL,
    ENTITY_WORKFLOW,
    REL_CONTAINS,
    REL_DEFINED_IN,
    REL_MAY_INVOKE,
    REL_MENTIONS,
    REL_NEXT,
    REL_REQUIRES,
    KnowledgeEdge,
    KnowledgeNode,
)

# Entities recognised in extracted text. Each is a domain noun the LLD names,
# matched only as a whole word so "preference" does not contain "reference".
_ENTITY_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (ENTITY_ROLE, "role", ("administrator", "admin", "finance team", "operator", "reviewer")),
    (
        ENTITY_COMPLIANCE,
        "compliance",
        ("gdpr", "hipaa", "pci dss", "soc 2", "iso 27001", "dpdp", "sox"),
    ),
    (
        ENTITY_REGION,
        "region",
        ("european union", "eu", "india", "united states", "us", "uk", "apac", "emea"),
    ),
)

_WORD = re.compile(r"[a-z0-9]+")


def normalize_key(text: str) -> str:
    """A stable identity for a node: lowercase words joined by hyphens."""
    return "-".join(_WORD.findall(text.lower()))[:120] or "unknown"


@dataclass
class GraphSummary:
    nodes_created: int = 0
    edges_created: int = 0
    degradations: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.degradations is None:
            self.degradations = []


class GraphBuilder:
    """Builds a document's slice of the graph inside one transaction."""

    def __init__(self, session: Session, org_id: uuid.UUID, document_id: uuid.UUID):
        self.session = session
        self.org_id = org_id
        self.document_id = document_id
        self._nodes: dict[tuple[str, str], KnowledgeNode] = {}
        self.summary = GraphSummary()

    def node(self, entity_type: str, key: str, label: str, **properties) -> KnowledgeNode:
        """Get or create a node. Idempotent, so re-processing updates rather
        than duplicating."""
        identity = (entity_type, normalize_key(key))
        if identity in self._nodes:
            return self._nodes[identity]

        existing = self.session.exec(
            select(KnowledgeNode)
            .where(KnowledgeNode.org_id == self.org_id)
            .where(KnowledgeNode.entity_type == entity_type)
            .where(KnowledgeNode.entity_key == identity[1])
        ).first()
        if existing is not None:
            existing.label = label or existing.label
            if properties:
                merged = json.loads(existing.properties_json or "{}")
                merged.update(properties)
                existing.properties_json = json.dumps(merged, default=str)
            existing.updated_at = datetime.utcnow()
            self.session.add(existing)
            self._nodes[identity] = existing
            return existing

        created = KnowledgeNode(
            org_id=self.org_id,
            entity_type=entity_type,
            entity_key=identity[1],
            label=label or key,
            properties_json=json.dumps(properties, default=str),
            document_id=self.document_id,
        )
        self.session.add(created)
        self.session.flush()
        self._nodes[identity] = created
        self.summary.nodes_created += 1
        return created

    def edge(
        self, source: KnowledgeNode, relationship: str, target: KnowledgeNode, **properties
    ) -> None:
        if source.id == target.id:
            return
        existing = self.session.exec(
            select(KnowledgeEdge)
            .where(KnowledgeEdge.org_id == self.org_id)
            .where(KnowledgeEdge.source_node_id == source.id)
            .where(KnowledgeEdge.relationship == relationship)
            .where(KnowledgeEdge.target_node_id == target.id)
        ).first()
        if existing is not None:
            return
        self.session.add(
            KnowledgeEdge(
                org_id=self.org_id,
                source_node_id=source.id,
                target_node_id=target.id,
                relationship=relationship,
                properties_json=json.dumps(properties, default=str),
                document_id=self.document_id,
            )
        )
        self.summary.edges_created += 1


def build_graph(
    session: Session,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    document_name: str,
    rules: list,
    workflows: list,
    terms: list,
) -> GraphSummary:
    """Turn extracted facts into nodes and edges.

    The document is the root: every fact it produced hangs off it, so "what did
    this document tell us" is one hop rather than a scan.
    """
    builder = GraphBuilder(session, org_id, document_id)
    document_node = builder.node(
        ENTITY_DOCUMENT, str(document_id), document_name or "Document", name=document_name
    )

    for rule in rules:
        rule_node = builder.node(
            ENTITY_BUSINESS_RULE,
            f"{document_id}:{rule.citation.start_offset}",
            (rule.action or rule.citation.text)[:120],
            rule_type=rule.rule_type,
            condition=rule.condition,
            action=rule.action,
            confidence=rule.confidence,
        )
        builder.edge(document_node, REL_CONTAINS, rule_node)
        for entity_type, key, label in _entities_in(rule.citation.text):
            entity = builder.node(entity_type, key, label)
            builder.edge(rule_node, REL_REQUIRES, entity)

    for workflow in workflows:
        workflow_node = builder.node(
            ENTITY_WORKFLOW,
            f"{document_id}:{workflow.name}:{workflow.citation.start_offset}",
            workflow.name,
            steps=len(workflow.nodes),
            confidence=workflow.confidence,
        )
        builder.edge(document_node, REL_CONTAINS, workflow_node)

        # Steps become nodes chained by NEXT, so the sequence is walkable
        # rather than a JSON blob nothing can query.
        previous = None
        for step in workflow.nodes:
            step_node = builder.node(
                ENTITY_WORKFLOW,
                f"{document_id}:{workflow.name}:step:{step['id']}",
                step["label"],
                order=step.get("order"),
                operation_id=step.get("operation_id"),
            )
            builder.edge(workflow_node, REL_CONTAINS, step_node)
            if previous is not None:
                builder.edge(previous, REL_NEXT, step_node)
            previous = step_node
            if step.get("operation_id"):
                tool_node = builder.node(ENTITY_TOOL, step["operation_id"], step["operation_id"])
                builder.edge(step_node, REL_MAY_INVOKE, tool_node)

    for term in terms:
        term_node = builder.node(
            ENTITY_TERM,
            term.term,
            term.term,
            definition=term.definition,
            confidence=term.confidence,
        )
        builder.edge(term_node, REL_DEFINED_IN, document_node)
        for entity_type, key, label in _entities_in(term.definition):
            entity = builder.node(entity_type, key, label)
            builder.edge(term_node, REL_MENTIONS, entity)

    return builder.summary


def _entities_in(text: str) -> list[tuple[str, str, str]]:
    """Domain entities named in a piece of text.

    Whole-word matching only, and only for the vocabulary the LLD names. This
    is deliberately narrow: a general entity recogniser would populate the
    graph with everything and mean nothing.
    """
    lowered = f" {text.lower()} "
    found = []
    for entity_type, _, keywords in _ENTITY_PATTERNS:
        for keyword in keywords:
            if f" {keyword} " in lowered or f" {keyword}." in lowered or f" {keyword}," in lowered:
                found.append((entity_type, keyword, keyword.title()))
    return found


def neighbours(
    session: Session, org_id: uuid.UUID, node_id: uuid.UUID, *, depth: int = 1, limit: int = 100
) -> dict:
    """Walk outward from a node.

    Bounded depth, because a graph query that can traverse the whole graph is a
    graph query that will, one day, on the largest tenant.
    """
    visited: set[uuid.UUID] = {node_id}
    frontier = [node_id]
    edges: list[KnowledgeEdge] = []

    for _ in range(max(depth, 1)):
        if not frontier:
            break
        found = session.exec(
            select(KnowledgeEdge)
            .where(KnowledgeEdge.org_id == org_id)
            .where(
                col(KnowledgeEdge.source_node_id).in_(frontier)
                | col(KnowledgeEdge.target_node_id).in_(frontier)
            )
            .limit(limit)
        ).all()
        next_frontier = []
        for edge in found:
            edges.append(edge)
            for candidate in (edge.source_node_id, edge.target_node_id):
                if candidate not in visited:
                    visited.add(candidate)
                    next_frontier.append(candidate)
        frontier = next_frontier

    nodes = session.exec(
        select(KnowledgeNode)
        .where(KnowledgeNode.org_id == org_id)
        .where(col(KnowledgeNode.id).in_(visited))
    ).all()
    return {"nodes": list(nodes), "edges": edges}
