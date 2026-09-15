"""The LLM extractor: declared, and unavailable until configured.

The LLD describes business rules as *LLM-classified* (§3.5), and a model is
genuinely better at the cases the pattern extractor misses — a rule spread
across two sentences, a limits table, a language other than English.

It is not implemented. Not because it is hard, but because implementing it
without a provider to run it against would mean writing a request shape,
a prompt, and a response parser that have never been executed once — and then
reporting the feature as present. Build prompt §4 and §83 both forbid exactly
that.

So this is the interface, the configuration it needs, and a refusal that names
what is missing. When a provider is configured, the work is: build the request,
parse the response into `ExtractedRule`s, and **keep the citation** — a model
that returns a rule without the sentence it came from has produced something
unverifiable, which is worse than nothing.
"""

from sutr.documentation.extraction.base import ExtractionResult, Extractor

VERSION = "llm-v0"

NOT_CONFIGURED = (
    "NOT_CONFIGURED: no LLM provider is configured for documentation extraction. "
    "The pattern-based extractor is used instead — it is deterministic and offline, "
    "and reads one sentence at a time. Configure a provider to extract rules that "
    "span sentences, live in tables, or are written in another language."
)


class LlmExtractor(Extractor):
    """Placeholder for model-based extraction."""

    id = "llm"
    version = VERSION
    display_name = "Model-based extractor"
    description = (
        "Extraction by a language model. Not implemented: it needs a configured provider "
        "to be written against, and shipping an unexercised request shape as a working "
        "feature would be a claim rather than a capability."
    )

    def available(self) -> tuple[bool, str | None]:
        return False, NOT_CONFIGURED

    def extract(self, chunks: list) -> ExtractionResult:
        result = ExtractionResult()
        result.degradations.append(
            {
                "code": "llm_extractor_unavailable",
                "message": NOT_CONFIGURED,
                "stage": "extract",
            }
        )
        return result
