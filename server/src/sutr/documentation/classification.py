"""What kind of document is this?

Knowing that a document is a policy rather than a runbook changes what is worth
extracting from it and how a search result should be presented, so the pipeline
labels documents rather than treating every upload as undifferentiated prose.

The classifier is deterministic: weighted signals — filename, headings, and
characteristic phrasing — with an explicit confidence and the matched signals
returned alongside the label. That last part is the point. A label with no
evidence is a guess wearing a badge; a label that can show the three headings
it matched can be argued with.

Deliberately *not* a model. A statistical classifier over a handful of
documents would be less accurate than these rules and much harder to explain,
and the build prompt's honesty requirement (§83) applies to confidence scores
as much as to feature lists.
"""

import re
from dataclasses import dataclass, field

TYPE_POLICY = "policy"
TYPE_API_GUIDE = "api_guide"
TYPE_RUNBOOK = "runbook"
TYPE_FAQ = "faq"
TYPE_GLOSSARY = "glossary"
TYPE_CONTRACT = "contract"
TYPE_RELEASE_NOTES = "release_notes"
TYPE_UNKNOWN = "unknown"

DOCUMENT_TYPES = (
    TYPE_POLICY,
    TYPE_API_GUIDE,
    TYPE_RUNBOOK,
    TYPE_FAQ,
    TYPE_GLOSSARY,
    TYPE_CONTRACT,
    TYPE_RELEASE_NOTES,
    TYPE_UNKNOWN,
)

# (document type, weight, pattern). Filename and heading matches are worth more
# than body matches: a document titled "Refund Policy" is a policy in a way
# that one merely mentioning the word "policy" is not.
_SIGNALS: tuple[tuple[str, float, re.Pattern], ...] = (
    (TYPE_POLICY, 3.0, re.compile(r"\bpolic(?:y|ies)\b")),
    (TYPE_POLICY, 2.0, re.compile(r"\b(?:must|shall|is prohibited|is not permitted)\b")),
    (TYPE_POLICY, 1.5, re.compile(r"\b(?:eligibilit|complian|entitle|permitted)")),
    (TYPE_API_GUIDE, 3.0, re.compile(r"\b(?:api|endpoint|sdk)\b")),
    (TYPE_API_GUIDE, 2.0, re.compile(r"\b(?:request|response|payload|status code|rate limit)\b")),
    (TYPE_API_GUIDE, 2.0, re.compile(r"\b(?:get|post|put|patch|delete)\s+/")),
    (TYPE_API_GUIDE, 1.5, re.compile(r"\b(?:authentication|bearer token|api key)\b")),
    (TYPE_RUNBOOK, 3.0, re.compile(r"\b(?:runbook|playbook|on-?call)\b")),
    (TYPE_RUNBOOK, 2.0, re.compile(r"\b(?:escalat|incident|mitigat|rollback|remediation)")),
    (TYPE_RUNBOOK, 1.5, re.compile(r"\bstep\s*\d+\b")),
    (TYPE_FAQ, 3.0, re.compile(r"\b(?:faq|frequently asked)\b")),
    (TYPE_FAQ, 2.0, re.compile(r"^\s*(?:q|question)\s*[:.]", re.MULTILINE | re.IGNORECASE)),
    (TYPE_GLOSSARY, 3.0, re.compile(r"\b(?:glossar|terminolog|definitions)")),
    (TYPE_GLOSSARY, 1.5, re.compile(r"\b(?:refers to|is defined as|means the)\b")),
    (TYPE_CONTRACT, 3.0, re.compile(r"\b(?:agreement|contract|terms of service|sla)\b")),
    (TYPE_CONTRACT, 2.0, re.compile(r"\b(?:hereinafter|the parties|governing law|indemnif)")),
    (TYPE_RELEASE_NOTES, 3.0, re.compile(r"\b(?:release notes|changelog)\b")),
    (TYPE_RELEASE_NOTES, 2.0, re.compile(r"\bv?\d+\.\d+\.\d+\b")),
    (TYPE_RELEASE_NOTES, 1.5, re.compile(r"\b(?:deprecat|breaking change|bug ?fix)")),
)

# Enough evidence to be worth stating. Below it the answer is "unknown", which
# is a real answer.
_MIN_SCORE = 4.0


@dataclass
class Classification:
    document_type: str = TYPE_UNKNOWN
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "document_type": self.document_type,
            "confidence": round(self.confidence, 3),
            "signals": self.signals,
            "scores": {key: round(value, 2) for key, value in sorted(self.scores.items())},
        }


def classify(
    *, filename: str = "", headings: list[str] | None = None, text: str = ""
) -> Classification:
    """Label a document from its name, its headings, and a sample of its text."""
    # Only the head of the document is scored. A 300-page contract that
    # mentions "endpoint" forty times in an appendix is still a contract, and
    # scanning all of it would let the tail outvote the title.
    body = text[:20_000].lower()
    title = filename.lower()
    heading_text = " \n ".join(headings or []).lower()

    scores: dict[str, float] = {}
    signals: list[str] = []
    for document_type, weight, pattern in _SIGNALS:
        hits = 0.0
        if pattern.search(title):
            hits += weight * 2  # the title is the strongest single signal
            signals.append(f"filename:{document_type}:{pattern.pattern[:40]}")
        if pattern.search(heading_text):
            hits += weight * 1.5
            signals.append(f"heading:{document_type}:{pattern.pattern[:40]}")
        found = len(pattern.findall(body))
        if found:
            # Saturating: the fifth mention says little the first did not.
            hits += weight * min(found, 5) / 5
            signals.append(f"body:{document_type}:{pattern.pattern[:40]}×{found}")
        if hits:
            scores[document_type] = scores.get(document_type, 0.0) + hits

    if not scores:
        return Classification(scores={})

    ranked = sorted(scores.items(), key=lambda entry: (-entry[1], entry[0]))
    best_type, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

    if best_score < _MIN_SCORE:
        return Classification(scores=scores, signals=signals)

    # Confidence is margin-based: a document that scores 9 for "policy" and 8
    # for "contract" is genuinely ambiguous, and should not report 0.9 just
    # because its winning score was high.
    margin = (best_score - runner_up) / best_score
    confidence = round(min(0.5 + margin / 2, 0.95), 3)
    return Classification(
        document_type=best_type,
        confidence=confidence,
        signals=[signal for signal in signals if f":{best_type}:" in signal],
        scores=scores,
    )
