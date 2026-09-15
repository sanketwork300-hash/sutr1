"""Linking a documented workflow's steps to the API operations they name.

LLD §3.5 asks a workflow to carry *linked operations/tools*, and build prompt
§17 wants the generated server to be built from *IR + documentation knowledge*.
Both need the same thing: the step "Verify Payment" in a prose workflow and the
operation `POST /payments/verify` have to be recognised as the same act.

This lives in the documentation service rather than in the generator because
`doc_workflow` is a documentation table and one service writes a table
(`platform/boundaries.py`). The generator asks for the link; the owner makes it.

The matching is deterministic token overlap, and it is deliberately
conservative:

- a step matches a tool when their significant tokens overlap by at least two,
  or when the step names the tool outright;
- ties are broken by score then by name, so the same inputs always produce the
  same links;
- **an unmatched step stays unmatched.** Attaching a step to the
  nearest-looking operation would produce a link that reads exactly like a
  verified one, and an agent following a wrong link does something wrong to
  somebody's API. Silence is the safe failure here.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from sutr.models.doc_workflow import DocWorkflow
from sutr.models.document import Document

# Words that carry no signal about *which* operation a step is. Verbs are
# deliberately NOT in here: "Create Pet" and "List Pets" differ by exactly the
# verb, and dropping it would leave both steps looking like the noun.
_STOPWORDS = frozenset(
    """
    a an and are as at be by for from in into is it of on or that the this to with
    step steps then next request response api call calls use using please new all any
    """.split()
)

_TOKEN = re.compile(r"[a-z0-9]+")

# Two overlapping tokens, because one is noise: "payment" alone matches every
# operation in a payments API.
MIN_TOKEN_OVERLAP = 2


def tokenize(text: str) -> set[str]:
    """Significant lowercase tokens, camelCase and snake_case split apart."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    return {
        token
        for token in _TOKEN.findall(spaced.lower())
        if token not in _STOPWORDS and len(token) > 2
    }


def operation_ref(tool: Any) -> str:
    """How an operation is named in a link.

    Method plus path, not operationId: operationId is optional in OpenAPI and a
    reference that is empty for half a specification is not a reference.
    """
    return f"{str(getattr(tool, 'method', '')).upper()} {getattr(tool, 'path', '')}".strip()


@dataclass
class StepLink:
    step: str
    tool: str
    operation: str
    score: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "tool": self.tool,
            "operation": self.operation,
            "score": self.score,
        }


@dataclass
class LinkReport:
    workflows: int = 0
    steps: int = 0
    linked_steps: int = 0
    links: list[StepLink] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflows": self.workflows,
            "steps": self.steps,
            "linked_steps": self.linked_steps,
            "unlinked_steps": self.steps - self.linked_steps,
            "links": [link.as_dict() for link in self.links],
        }


def tool_tokens(tool: Any) -> set[str]:
    """The tokens that identify one tool: its name, first sentence and path."""
    parts = [
        getattr(tool, "name", ""),
        getattr(tool, "description", "").split(".")[0],
        getattr(tool, "path", "").replace("/", " ").replace("{", " ").replace("}", " "),
    ]
    return tokenize(" ".join(parts))


def match_step(step: str, tools: list[Any]) -> tuple[Any, int] | None:
    """The best tool for one step, or None when nothing is convincing."""
    step_tokens = tokenize(step)
    if not step_tokens:
        return None
    best: tuple[Any, int] | None = None
    for tool in sorted(tools, key=lambda item: getattr(item, "name", "")):
        name = getattr(tool, "name", "")
        overlap = len(step_tokens & tool_tokens(tool))
        # Naming the tool outright is decisive whatever the token count says.
        # Compared token-wise rather than as a substring, so "Create Pet"
        # matches `create_pet` the way a reader would expect it to.
        name_parts = tokenize(name)
        if name_parts and name_parts <= step_tokens:
            overlap = max(overlap, MIN_TOKEN_OVERLAP + 1)
        if overlap >= MIN_TOKEN_OVERLAP and (best is None or overlap > best[1]):
            best = (tool, overlap)
    return best


def link_workflows(
    session: Session,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
    tools: list[Any],
    commit: bool = False,
) -> LinkReport:
    """Populate `operation_refs` and `tool_refs` on this project's workflows.

    Scoped to the org on the query, and to documents belonging to the project,
    so one tenant's workflows can never be annotated with another's tools.
    """
    report = LinkReport()
    if not tools:
        return report

    statement = select(DocWorkflow).where(DocWorkflow.org_id == org_id)
    if project_id is not None:
        document_ids = session.exec(
            select(Document.id).where(Document.org_id == org_id, Document.project_id == project_id)
        ).all()
        if not document_ids:
            return report
        statement = statement.where(DocWorkflow.document_id.in_(document_ids))  # type: ignore[attr-defined]

    for workflow in session.exec(statement).all():
        report.workflows += 1
        nodes = json.loads(workflow.nodes_json or "[]")
        operations: list[str] = []
        tool_names: list[str] = []
        for node in nodes:
            label = node.get("label", "")
            report.steps += 1
            matched = match_step(label, tools)
            if matched is None:
                continue
            tool, score = matched
            report.linked_steps += 1
            reference = operation_ref(tool)
            name = getattr(tool, "name", "")
            if reference not in operations:
                operations.append(reference)
            if name not in tool_names:
                tool_names.append(name)
            report.links.append(StepLink(step=label, tool=name, operation=reference, score=score))
        workflow.operation_refs_json = json.dumps(operations)
        workflow.tool_refs_json = json.dumps(tool_names)
        session.add(workflow)

    if commit:
        session.commit()
    return report
