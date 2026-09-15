import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel

# The categories build prompt §16 fixes.
TYPE_VALIDATION = "validation"
TYPE_ELIGIBILITY = "eligibility"
TYPE_LIMITS = "limits"
TYPE_COMPLIANCE = "compliance"
TYPE_APPROVAL = "approval"
TYPE_EXCEPTIONS = "exceptions"
TYPE_SECURITY = "security"
TYPE_PRICING = "pricing"
TYPE_REGIONAL = "regional"
TYPE_ROLE_BASED = "role_based"

RULE_TYPES = (
    TYPE_VALIDATION,
    TYPE_ELIGIBILITY,
    TYPE_LIMITS,
    TYPE_COMPLIANCE,
    TYPE_APPROVAL,
    TYPE_EXCEPTIONS,
    TYPE_SECURITY,
    TYPE_PRICING,
    TYPE_REGIONAL,
    TYPE_ROLE_BASED,
)


class BusinessRule(SQLModel, table=True):
    """A rule stated in prose, turned into something structured.

    The LLD's own example (§3.5): *"Refund allowed only within 30 days"* →
    `{type: BusinessRule, condition: "Purchase ≤ 30 days", action: "Allow
    Refund"}`.

    Build prompt §16 fixes the field set and adds the rule that shapes this
    whole table: **never lose the source citation**. Every row points back at
    the document, the chunk, the character offsets and the sentence it came
    from — because a rule an agent acts on that nobody can trace back to a
    sentence is a rule nobody can check.

    `confidence` is a heuristic, not a probability. `extractor_version` says
    what produced it, so a rule found by pattern matching is distinguishable
    from one found by a model, and neither is mistaken for the other.
    """

    __tablename__ = "business_rule"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    document_id: uuid.UUID = Field(foreign_key="document.id", index=True)
    chunk_id: uuid.UUID | None = Field(default=None, foreign_key="document_chunk.id")
    job_id: uuid.UUID | None = Field(default=None, foreign_key="document_job.id")

    rule_type: str = Field(default=TYPE_VALIDATION, index=True)
    # The circumstance under which it applies.
    condition: str = ""
    # What follows when it does.
    action: str = ""

    # ── Citation. Not optional, by design. ───────────────────────────────────
    source_document: str = ""
    source_location: str = ""
    # The sentence, verbatim. A paraphrase would be a second thing to verify.
    source_text: str = ""
    start_offset: int = Field(default=0)
    end_offset: int = Field(default=0)
    page: int | None = None

    confidence: float = Field(default=0.0)
    extractor_version: str = ""
    # Which signals fired, so a low confidence score can be argued with.
    signals_json: str = Field(default="[]")

    created_at: datetime = Field(default_factory=datetime.utcnow)
