"""Finding processes described in prose.

The LLD's example (§3.5): *Refund Request → Verify Payment → Eligibility Check
→ Approval → Refund → Notification*.

People write processes in three shapes, and all three are recognised here:

1. **An arrow chain**, exactly as the LLD writes it — `A → B → C`, or with
   `->`. Unambiguous, so it needs no heuristics.
2. **A numbered list** under a heading. "1. Verify the payment. 2. Check
   eligibility." The numbering *is* the ordering.
3. **A sequence of ordinal cues** in prose — "First… then… next… finally".

What is deliberately *not* done: inferring a workflow from a bulleted list.
A bulleted list is usually a set of requirements, not a sequence, and treating
one as a process invents an ordering the author never stated.

Steps are matched to API operations by name where the match is unambiguous.
An unmatched step stays unmatched — attaching it to the nearest-looking
operation would be a guess presented as a link.
"""

import re

from sutr.documentation.extraction.base import Citation, ExtractedWorkflow
from sutr.documentation.extraction.layout import is_layout_noise, is_running_header

VERSION = "rule-based-v1"

_ARROW = re.compile(r"\s*(?:→|->|—>|➜|=>)\s*")
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_ORDINALS = (
    "first",
    "then",
    "next",
    "after that",
    "afterwards",
    "subsequently",
    "finally",
    "lastly",
)
# A step label longer than this is a paragraph, not a step name.
MAX_STEP_LABEL = 120
MIN_STEPS = 2
MAX_STEPS = 30

# Words that make a heading look like a process rather than a description.
_PROCESS_WORDS = (
    "workflow",
    "process",
    "steps",
    "procedure",
    "lifecycle",
    "life cycle",
    "flow",
    "how it works",
    "sequence",
)

# A PDF of an architecture diagram flattens to lines that look exactly like
# arrow chains — because they *are* arrows, drawn between boxes. See
# `layout.py` for what is rejected and why.


def _clean(label: str) -> str:
    return " ".join(label.strip(" .;:-–—\t").split())[:MAX_STEP_LABEL]


def _nodes_and_edges(labels: list[str]) -> tuple[list[dict], list[dict]]:
    nodes = [
        {"id": f"n{index}", "label": label, "order": index} for index, label in enumerate(labels)
    ]
    edges = [
        {"from": f"n{index}", "to": f"n{index + 1}", "condition": None}
        for index in range(len(labels) - 1)
    ]
    return nodes, edges


def _citation(chunk, text: str) -> Citation:
    from sutr.documentation.chunking import locate

    # Whitespace-tolerant: the matched text has had hard wrapping flattened,
    # so an exact search would miss and silently cite offset 0.
    begin, end = locate(chunk.text, text)
    return Citation(
        chunk_id=str(getattr(chunk, "id", "") or "") or None,
        section_path=getattr(chunk, "section_path", ""),
        start_offset=chunk.start_offset + begin,
        end_offset=chunk.start_offset + end,
        page=getattr(chunk, "page", None),
        text=text,
    )


def _name_for(chunk, labels: list[str]) -> str:
    """A workflow's name: its section heading, or its first step.

    A running page header is not a section heading. PDFs repeat one on every
    page, and the parser cannot tell it from a title — so a chain found under
    one is named after its first step instead, which at least describes it.
    """
    section = (getattr(chunk, "section_path", "") or "").split(" > ")[-1].strip()
    if section and not is_running_header(section):
        return section
    return labels[0] if labels else "Workflow"


def _from_arrow_chain(chunk) -> list[ExtractedWorkflow]:
    workflows = []
    for line in chunk.text.splitlines():
        if not _ARROW.search(line) or is_layout_noise(line):
            continue
        labels = [_clean(part) for part in _ARROW.split(line) if _clean(part)]
        if MIN_STEPS <= len(labels) <= MAX_STEPS:
            nodes, edges = _nodes_and_edges(labels)
            workflows.append(
                ExtractedWorkflow(
                    name=_name_for(chunk, labels),
                    nodes=nodes,
                    edges=edges,
                    citation=_citation(chunk, line.strip()),
                    description=line.strip(),
                    # An arrow chain states the sequence outright; there is
                    # nothing being inferred.
                    confidence=0.9,
                )
            )
    return workflows


def _from_numbered_list(chunk) -> list[ExtractedWorkflow]:
    labels: list[str] = []
    seen_numbers: list[int] = []
    for line in chunk.text.splitlines():
        match = _NUMBERED.match(line)
        if not match:
            continue
        seen_numbers.append(int(match.group(1)))
        labels.append(_clean(match.group(2)))

    if not (MIN_STEPS <= len(labels) <= MAX_STEPS):
        return []
    # Consecutive numbering is what distinguishes a process from a numbered set
    # of unrelated notes.
    if seen_numbers != list(range(seen_numbers[0], seen_numbers[0] + len(seen_numbers))):
        return []

    section = (getattr(chunk, "section_path", "") or "").lower()
    looks_like_process = any(word in section for word in _PROCESS_WORDS)
    nodes, edges = _nodes_and_edges(labels)
    return [
        ExtractedWorkflow(
            name=_name_for(chunk, labels),
            nodes=nodes,
            edges=edges,
            citation=_citation(chunk, chunk.text[:400]),
            description=f"{len(labels)} steps",
            # A numbered list under a heading that says "process" is almost
            # certainly one; the same list elsewhere might be anything.
            confidence=0.75 if looks_like_process else 0.55,
        )
    ]


def _from_ordinals(chunk) -> list[ExtractedWorkflow]:
    from sutr.documentation.chunking import split_sentences

    sentences = split_sentences(chunk.text)
    steps: list[str] = []
    for sentence in sentences:
        lowered = sentence.lower().lstrip()
        if any(lowered.startswith(f"{word} ") or f", {word} " in lowered for word in _ORDINALS):
            steps.append(_clean(sentence))

    if not (MIN_STEPS <= len(steps) <= MAX_STEPS):
        return []
    nodes, edges = _nodes_and_edges(steps)
    return [
        ExtractedWorkflow(
            name=_name_for(chunk, steps),
            nodes=nodes,
            edges=edges,
            citation=_citation(chunk, chunk.text[:400]),
            description=f"{len(steps)} steps described in sequence",
            # Ordinal cues in prose are the weakest of the three signals.
            confidence=0.45,
        )
    ]


def extract_workflows(chunks: list) -> list[ExtractedWorkflow]:
    workflows: list[ExtractedWorkflow] = []
    for chunk in chunks:
        if getattr(chunk, "element_type", "") == "code":
            continue
        found = _from_arrow_chain(chunk)
        if not found:
            found = _from_numbered_list(chunk)
        if not found:
            found = _from_ordinals(chunk)
        workflows += found
    return workflows


def match_operations(workflow: ExtractedWorkflow, definition) -> tuple[list[str], list[str]]:
    """Link a workflow's steps to API operations, where the link is unambiguous.

    Matching is on normalized words: a step "Verify Payment" matches an
    operation `verifyPayment` or `POST /payments/verify`. A step that matches
    nothing stays unmatched — the LLD wants workflows *associated with*
    operations, and a wrong association is worse than none.
    """
    if definition is None:
        return [], []

    def words(text: str) -> frozenset[str]:
        return frozenset(re.findall(r"[a-z0-9]+", text.lower()))

    candidates = []
    for operation in definition.operations:
        identity = " ".join(
            filter(None, [operation.operation_id or "", operation.summary or "", operation.path])
        )
        candidates.append((operation, words(identity)))

    matched_operations: list[str] = []
    for node in workflow.nodes:
        step_words = words(node["label"]) - {"the", "a", "an", "of", "to", "and", "for"}
        if not step_words:
            continue
        best = None
        best_overlap = 0
        for operation, operation_words in candidates:
            overlap = len(step_words & operation_words)
            if overlap > best_overlap:
                best, best_overlap = operation, overlap
        # Two shared words, or one that is the whole step, is a match. One
        # incidental shared word is not.
        if best is not None and (best_overlap >= 2 or (best_overlap == 1 and len(step_words) == 1)):
            if best.operation_id and best.operation_id not in matched_operations:
                matched_operations.append(best.operation_id)
                node["operation_id"] = best.operation_id

    return matched_operations, []
