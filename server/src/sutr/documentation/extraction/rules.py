"""Finding business rules in prose, deterministically.

The LLD describes rule extraction as LLM-classified (§3.5), and a model is the
right tool for the hard cases. It is also an external dependency, a cost per
document, and a source of confident invention — and this platform's first rule
is that nothing is fabricated. So the default extractor is **deterministic**:
it finds rules by the grammar people actually write them in, and it is honest
that this is what it does.

What makes a sentence a rule rather than a description is a **deontic marker** —
must, shall, may not, is required, are allowed, is limited to. Documentation is
full of sentences describing behaviour; only some of them impose a constraint,
and the modal verb is how English marks the difference:

    "The refund endpoint returns a receipt."          description
    "Refunds must be requested within 30 days."       rule

A rule is then split into **condition** (when it applies) and **action** (what
follows), because that is the shape the LLD's own example takes:

    "Refund allowed only within 30 days"
      → condition: "within 30 days", action: "Refund allowed"

Everything here is a heuristic, and the confidence score says so — it is a
count of signals that fired, normalized, not a probability. The signals
themselves are stored on every rule so a low score can be argued with rather
than merely distrusted.

Limits of this approach, stated rather than discovered: it reads one sentence
at a time, so a rule spread over two sentences is missed; it is English-only;
and a table of limits is read as prose. An LLM extractor addresses all three,
and slots in behind the same interface.
"""

import re

from sutr.documentation.chunking import locate, split_sentences
from sutr.documentation.extraction.base import Citation, ExtractedRule, ExtractionResult, Extractor
from sutr.documentation.extraction.layout import is_layout_noise
from sutr.models.business_rule import (
    TYPE_APPROVAL,
    TYPE_COMPLIANCE,
    TYPE_ELIGIBILITY,
    TYPE_EXCEPTIONS,
    TYPE_LIMITS,
    TYPE_PRICING,
    TYPE_REGIONAL,
    TYPE_ROLE_BASED,
    TYPE_SECURITY,
    TYPE_VALIDATION,
)

VERSION = "rule-based-v1"

# Deontic markers: what turns a description into a constraint.
_MODALS = re.compile(
    r"\b("
    r"must(?:\s+not)?|shall(?:\s+not)?|may(?:\s+not)?|cannot|can(?:'|’)?t|"
    r"should(?:\s+not)?|is\s+required|are\s+required|is\s+not\s+permitted|"
    r"are\s+not\s+permitted|is\s+permitted|are\s+permitted|is\s+allowed|"
    r"are\s+allowed|is\s+prohibited|are\s+prohibited|is\s+limited|are\s+limited|"
    r"is\s+restricted|are\s+restricted|is\s+forbidden|will\s+be\s+rejected|"
    r"is\s+rejected|are\s+rejected|is\s+eligible|are\s+eligible|is\s+subject\s+to|"
    r"are\s+subject\s+to|requires?|needs?\s+to"
    r")\b",
    re.IGNORECASE,
)

# Clauses that state *when* a rule applies. Ordered longest-first so
# "provided that" wins over "provided".
_CONDITION_MARKERS = (
    "provided that",
    "on condition that",
    "as long as",
    "only if",
    "only when",
    "only within",
    "in the event that",
    "subject to",
    "unless",
    "if",
    "when",
    "where",
    "after",
    "before",
    "within",
    "during",
    "for customers",
    "for accounts",
)

# A quantified window or amount is strong evidence of a limit.
_QUANTITY = re.compile(
    r"\b\d[\d,._]*\s*(?:%|percent|days?|hours?|minutes?|seconds?|months?|years?|"
    r"requests?|calls?|items?|records?|bytes?|kb|mb|gb|usd|eur|gbp|inr)\b",
    re.IGNORECASE,
)
_CURRENCY = re.compile(r"[$€£₹]\s?\d")

# Category signals. A sentence may match several; the strongest wins, and ties
# break toward the more specific category.
_CATEGORY_SIGNALS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    (
        TYPE_COMPLIANCE,
        (
            "gdpr",
            "hipaa",
            "pci dss",
            "pci-dss",
            "sox",
            "iso 27001",
            "soc 2",
            "dpdp",
            "rbi",
            "regulation",
            "regulatory",
            "audit trail",
            "retention period",
            "data residency",
            "personally identifiable",
            "compliance",
        ),
        3,
    ),
    (
        TYPE_SECURITY,
        (
            "authenticate",
            "authentication",
            "authorization header",
            "api key",
            "access token",
            "oauth",
            "encrypt",
            "tls",
            "mfa",
            "two-factor",
            "credential",
            "permission",
            "scope",
            "signature",
        ),
        3,
    ),
    (
        TYPE_APPROVAL,
        (
            "approval",
            "approved by",
            "authorize",
            "authorised",
            "sign-off",
            "sign off",
            "reviewed by",
            "manual review",
        ),
        3,
    ),
    (
        TYPE_EXCEPTIONS,
        ("exempt", "exception", "waiver", "waived", "does not apply", "excluding", "except"),
        3,
    ),
    (
        # A prohibition carves an exception out of a general permission:
        # "refunds are allowed" / "refunds are not permitted for digital
        # goods". Weighted below the explicit signals above so that a sentence
        # prohibiting something *on security grounds* still classifies as
        # security, which is the more useful label.
        TYPE_EXCEPTIONS,
        (
            "not permitted",
            "not allowed",
            "not be permitted",
            "prohibited",
            "must not",
            "shall not",
            "may not",
            "cannot be",
            "is not eligible",
            "are not eligible",
            "ineligible",
        ),
        2,
    ),
    (
        TYPE_PRICING,
        (
            "fee",
            "fees",
            "charge",
            "charged",
            "price",
            "pricing",
            "cost",
            "billed",
            "invoice",
            "surcharge",
            "tax",
        ),
        3,
    ),
    (
        TYPE_REGIONAL,
        (
            "region",
            "regional",
            "country",
            "countries",
            "jurisdiction",
            "residents of",
            "european union",
            "eu only",
            "in india",
            "outside the",
        ),
        3,
    ),
    (
        TYPE_ROLE_BASED,
        (
            "role",
            "roles",
            "administrator",
            "admin users",
            "finance team",
            "operator",
            "member of",
            "permission group",
            "only staff",
        ),
        3,
    ),
    (
        TYPE_ELIGIBILITY,
        (
            "eligible",
            "eligibility",
            "qualify",
            "qualifies",
            "entitled",
            "in good standing",
            "active account",
            "active subscription",
        ),
        2,
    ),
    (
        TYPE_LIMITS,
        (
            "maximum",
            "minimum",
            "at most",
            "at least",
            "no more than",
            "no fewer than",
            "up to",
            "limit",
            "limited to",
            "exceed",
            "cap",
            "quota",
            "rate limit",
            "per day",
            "per month",
            "per hour",
        ),
        2,
    ),
    (
        TYPE_VALIDATION,
        (
            "valid",
            "invalid",
            "format",
            "must match",
            "well-formed",
            "non-empty",
            "required field",
            "mandatory",
            "pattern",
        ),
        1,
    ),
)

# Sentences that are examples or headings rather than statements.
_NOISE = re.compile(r"^(?:for example|e\.g\.|note:|tip:|warning:|see also)\b", re.IGNORECASE)
MIN_SENTENCE_CHARS = 25
MAX_SENTENCE_CHARS = 600


def _classify(sentence: str) -> tuple[str, list[str], int]:
    """The rule's category, the signals that fired, and their weight."""
    lowered = sentence.lower()
    best_type = TYPE_VALIDATION
    best_weight = 0
    signals: list[str] = []

    for rule_type, keywords, weight in _CATEGORY_SIGNALS:
        matched = [keyword for keyword in keywords if keyword in lowered]
        if not matched:
            continue
        signals += [f"{rule_type}:{keyword}" for keyword in matched[:3]]
        score = weight + (len(matched) - 1)
        if score > best_weight:
            best_weight, best_type = score, rule_type

    return best_type, signals, best_weight


def _split_condition(sentence: str) -> tuple[str, str]:
    """Separate 'when it applies' from 'what follows'.

    Returns (condition, action). An unconditional rule has an empty condition —
    which is meaningfully different from a rule whose condition was missed, so
    it is left empty rather than filled with the sentence.
    """
    lowered = sentence.lower()
    for marker in _CONDITION_MARKERS:
        index = lowered.find(f" {marker} ")
        if index == -1 and lowered.startswith(f"{marker} "):
            index = 0
        if index == -1:
            continue

        if index == 0:
            # "If X, then Y" — the condition leads.
            remainder = sentence[len(marker) :].lstrip()
            comma = remainder.find(",")
            if comma > 0:
                return remainder[:comma].strip(), remainder[comma + 1 :].strip(" .")
            return remainder.strip(" ."), ""
        # "Y within 30 days" — the condition trails.
        action = sentence[:index].strip(" ,.")
        condition = sentence[index + 1 :].strip(" .")
        return condition, action

    return "", sentence.strip(" .")


def _confidence(has_modal: bool, weight: int, has_quantity: bool, has_condition: bool) -> float:
    """A count of signals, normalized. Not a probability, and not presented as one."""
    score = 0.35 if has_modal else 0.0
    score += min(weight, 4) * 0.1
    if has_quantity:
        score += 0.15
    if has_condition:
        score += 0.1
    return round(min(score, 0.95), 2)


def extract_rules_from_sentence(sentence: str, citation: Citation) -> ExtractedRule | None:
    """One sentence → at most one rule."""
    text = sentence.strip()
    if not (MIN_SENTENCE_CHARS <= len(text) <= MAX_SENTENCE_CHARS):
        return None
    if _NOISE.match(text):
        return None

    has_modal = bool(_MODALS.search(text))
    rule_type, signals, weight = _classify(text)
    has_quantity = bool(_QUANTITY.search(text) or _CURRENCY.search(text))

    # A sentence with no modal is a description unless it carries a strong
    # category signal *and* a quantity — "The maximum refund is 10,000 USD"
    # states a limit without a modal verb.
    if not has_modal and not (weight >= 2 and has_quantity):
        return None

    condition, action = _split_condition(text)
    if not action:
        action = text.strip(" .")

    # A rule whose condition is a window or an amount is a limit, whatever else
    # it says. "Refunds are allowed only within 30 days" carries no category
    # keyword at all, and calling it a validation rule would be worse than
    # calling it what it is.
    if rule_type == TYPE_VALIDATION and weight <= 1 and has_quantity and condition:
        rule_type = TYPE_LIMITS
        signals.append("limits:quantified-condition")

    return ExtractedRule(
        rule_type=rule_type,
        condition=condition,
        action=action,
        citation=citation,
        confidence=_confidence(has_modal, weight, has_quantity, bool(condition)),
        signals=(["modal"] if has_modal else []) + (["quantity"] if has_quantity else []) + signals,
    )


class RuleBasedExtractor(Extractor):
    """The default extractor: deterministic, offline, and explicit about it."""

    id = "rule_based"
    version = VERSION
    display_name = "Pattern-based extractor"
    description = (
        "Finds rules, workflows and terms by the grammar they are written in. Deterministic "
        "and needs no external service. Reads one sentence at a time, so a rule spread across "
        "two sentences is missed, and it is English-only."
    )

    def extract(self, chunks: list) -> ExtractionResult:
        from sutr.documentation.extraction.glossary import extract_terms
        from sutr.documentation.extraction.workflows import extract_workflows

        result = ExtractionResult()
        for chunk in chunks:
            result.rules += self._rules_in(chunk)
        result.workflows += extract_workflows(chunks)
        result.terms += extract_terms(chunks)
        return result

    def _rules_in(self, chunk) -> list[ExtractedRule]:
        # Code blocks and tables are not prose; running sentence patterns over
        # a JSON sample produces confident nonsense.
        if getattr(chunk, "element_type", "") in ("code", "table"):
            return []

        rules: list[ExtractedRule] = []
        offset = chunk.start_offset
        for sentence in split_sentences(chunk.text):
            # A flattened table row is not a sentence, and a rule citing one
            # would point at two cells that happened to land on the same line
            # — a citation nobody can check (see `layout.py`).
            if is_layout_noise(sentence):
                continue
            # Whitespace-tolerant: `split_sentences` flattens hard wrapping, so
            # the sentence need not appear verbatim in the chunk.
            begin, end = locate(chunk.text, sentence)
            citation = Citation(
                chunk_id=str(getattr(chunk, "id", "") or "") or None,
                section_path=getattr(chunk, "section_path", ""),
                start_offset=offset + begin,
                end_offset=offset + end,
                page=getattr(chunk, "page", None),
                text=sentence,
            )
            rule = extract_rules_from_sentence(sentence, citation)
            if rule is not None:
                rules.append(rule)
        return rules
