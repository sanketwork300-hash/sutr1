"""One interface for every place an API definition can live.

ESDS LLD §3.3: onboarding never starts with "upload your OpenAPI", it starts
with *"Where does your API definition currently live?"* — and every connector
feeds the same normalization layer, so *"the compiler never knows — or cares —
where a spec came from"*.

That last property is the one worth protecting. A connector's entire job is to
turn a locator into raw text plus provenance. It does not parse, validate, or
normalize; those are the translation service's job, and a connector that starts
doing them is a connector that will disagree with the others.

Build prompt §12 fixes the seven verbs. Four are connector-specific and
abstract; three (`validate`, `detect_drift`, `disconnect`) have one correct
implementation for everybody and are provided here, so every connector really
does have all seven rather than each reinventing three of them.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConfigField:
    """One thing a connector must be told. Rendered by the console."""

    key: str
    label: str
    kind: str = "text"  # text | url | secret | select
    required: bool = True
    placeholder: str = ""
    help: str = ""
    options: tuple[str, ...] = ()


@dataclass
class Provenance:
    """Where a document came from, and which version of it this is.

    Build prompt §13 fixes this field set. The version fields are what make a
    conditional fetch possible: sending the last `etag` or `commit_sha` back to
    the source lets it answer "nothing changed" without transferring anything,
    which is the difference between polling being cheap and being rude.
    """

    source_type: str = ""
    source_uri: str = ""
    # The source's own name for this version: a git commit, a SwaggerHub
    # version, an ETag. Opaque to us; meaningful to the source.
    source_version: str | None = None
    commit_sha: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    retrieved_at: str | None = None
    # Connector-specific extras (repository, branch, path, filename, ...).
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_uri": self.source_uri,
            "source_version": self.source_version,
            "commit_sha": self.commit_sha,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "retrieved_at": self.retrieved_at,
            "detail": self.detail,
        }


@dataclass
class FetchResult:
    """What a fetch produced.

    `not_modified` is not an error and not an empty document: it is the source
    saying "you already have this". Conflating it with either would make every
    poll look like a change or like a failure.
    """

    content: str | None
    provenance: Provenance
    not_modified: bool = False


@dataclass
class Discovered:
    """One candidate definition found at a source."""

    identifier: str  # what to pass back as `path`/`selection`
    label: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"identifier": self.identifier, "label": self.label, "detail": self.detail}


@dataclass
class ConnectionResult:
    """Whether a source is reachable and usable as configured."""

    connected: bool
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


# How a connector learns that a source changed.
WATCH_NONE = "none"  # the content is pushed to us; there is nothing to watch
WATCH_POLL = "poll"  # ask the source periodically, conditionally where possible
WATCH_WEBHOOK = "webhook"  # the source can call us


@dataclass
class WatchPlan:
    """How this source should be watched, and how cheaply."""

    mode: str = WATCH_NONE
    # Suggested seconds between polls. Ignored for webhook-only sources.
    interval_seconds: int = 3600
    # True when the source supports a conditional request (ETag, commit sha),
    # so a poll that finds nothing costs almost nothing.
    conditional: bool = False
    reason: str = ""


class ConnectorError(Exception):
    """A connector operation failed for a reason the user can act on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SourceConnector(ABC):
    """A place API definitions live."""

    id: str
    display_name: str
    description: str = ""
    config_fields: tuple[ConfigField, ...] = ()
    # False for connectors whose content is handed to us (paste, upload):
    # there is nothing to re-fetch, so there is nothing to watch.
    supports_watch: bool = False
    supports_discovery: bool = False
    # Set on a connector that is declared but not implemented, with the reason.
    unavailable_reason: str | None = None

    # ── The four verbs a connector must implement ────────────────────────────

    @abstractmethod
    async def connect(self, config: dict, secrets: dict) -> ConnectionResult:
        """Check the source is reachable and the configuration is usable.

        Runs before anything is stored, so a mistyped URL or a missing token is
        reported while the user is still looking at the form.
        """

    @abstractmethod
    async def fetch(
        self, config: dict, secrets: dict, *, known: Provenance | None = None
    ) -> FetchResult:
        """Retrieve the current document.

        `known` is the provenance of what the platform already holds. A
        connector that can make a conditional request should use it and return
        `not_modified=True` rather than transferring the document again.
        """

    @abstractmethod
    def watch_plan(self, config: dict) -> WatchPlan:
        """How this source should be watched for changes."""

    async def discover(self, config: dict, secrets: dict) -> list[Discovered]:
        """Candidate definitions at this source.

        Empty by default: most sources point at exactly one document, and
        inventing candidates for them would be noise.
        """
        return []

    async def disconnect(self, config: dict, secrets: dict) -> None:
        """Release anything held on the source's side.

        A no-op for every connector that only reads — which is all of them
        today. It exists because a connector that *does* register a webhook
        must have somewhere to remove it, and discovering that later would mean
        changing the interface after implementations exist.
        """
        return None

    # ── The three verbs with one correct implementation ─────────────────────

    def validate(self, content: str) -> "ValidationResult":
        """Check the fetched text really is a specification we can translate.

        Identical for every connector by construction: a document's validity
        has nothing to do with where it was found. Implemented here rather than
        per connector so it cannot drift.
        """
        from sutr.source_connectors.validation import validate_document

        return validate_document(content)

    def detect_drift(self, before, after):
        """What changed between two IRs.

        Also identical for every connector, and also implemented once: drift is
        a property of the API, not of the transport that delivered it.
        """
        from sutr.openapi.diff import compare

        return compare(before, after)

    # ── Description ──────────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "supports_watch": self.supports_watch,
            "supports_discovery": self.supports_discovery,
            "available": self.unavailable_reason is None,
            "unavailable_reason": self.unavailable_reason,
            "config_fields": [
                {
                    "key": field_spec.key,
                    "label": field_spec.label,
                    "kind": field_spec.kind,
                    "required": field_spec.required,
                    "placeholder": field_spec.placeholder,
                    "help": field_spec.help,
                    "options": list(field_spec.options),
                }
                for field_spec in self.config_fields
            ],
        }


@dataclass
class ValidationResult:
    """Whether a fetched document can be translated, and what is wrong if not."""

    valid: bool
    error_code: str | None = None
    message: str = ""
    # Findings from the lint layer, when the document parsed at all.
    findings: list = field(default_factory=list)
    api_title: str | None = None
    api_version: str | None = None
    operation_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "error_code": self.error_code,
            "message": self.message,
            "findings": [finding.model_dump(mode="json") for finding in self.findings],
            "api_title": self.api_title,
            "api_version": self.api_version,
            "operation_count": self.operation_count,
        }
