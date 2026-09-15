"""Sources whose content is handed to us: paste and upload.

There is nothing to re-fetch and therefore nothing to watch. The LLD's own
framing (§3.3) puts these under "for demos, pilots, small teams, hackathons" —
one authoritative document, imported once, done. A startup uploads once and is
finished, and the platform should not pretend to watch a file on someone's
laptop.
"""

from datetime import datetime, timezone

from sutr.source_connectors.base import (
    WATCH_NONE,
    ConfigField,
    ConnectionResult,
    ConnectorError,
    FetchResult,
    Provenance,
    SourceConnector,
    WatchPlan,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PasteConnector(SourceConnector):
    id = "paste"
    display_name = "Paste a specification"
    description = "The document itself, pasted in. Nothing is fetched, so nothing is watched."
    config_fields = (
        ConfigField(
            key="content",
            label="Specification",
            kind="text",
            help="The OpenAPI or Swagger document, as JSON or YAML.",
        ),
    )

    async def connect(self, config: dict, secrets: dict) -> ConnectionResult:
        if not (config.get("content") or "").strip():
            return ConnectionResult(connected=False, message="No content was provided.")
        return ConnectionResult(connected=True, message="Content accepted.")

    async def fetch(self, config: dict, secrets: dict, *, known: Provenance | None = None):
        content = config.get("content")
        if not content:
            raise ConnectorError("no_content", "This source holds no content.")
        return FetchResult(
            content=content,
            provenance=Provenance(source_type=self.id, source_uri="", retrieved_at=_now()),
        )

    def watch_plan(self, config: dict) -> WatchPlan:
        return WatchPlan(
            mode=WATCH_NONE,
            reason="Pasted content cannot change on its own; re-paste to update it.",
        )


class UploadConnector(SourceConnector):
    id = "upload"
    display_name = "Upload a file"
    description = "A file uploaded once. The filename is kept as provenance."
    config_fields = (
        ConfigField(key="content", label="File contents", kind="text"),
        ConfigField(key="filename", label="File name", required=False),
    )

    async def connect(self, config: dict, secrets: dict) -> ConnectionResult:
        if not (config.get("content") or "").strip():
            return ConnectionResult(connected=False, message="The uploaded file is empty.")
        return ConnectionResult(
            connected=True, message=f"Accepted {config.get('filename') or 'the upload'}."
        )

    async def fetch(self, config: dict, secrets: dict, *, known: Provenance | None = None):
        content = config.get("content")
        if not content:
            raise ConnectorError("no_content", "This source holds no content.")
        return FetchResult(
            content=content,
            provenance=Provenance(
                source_type=self.id,
                source_uri=config.get("filename") or "",
                retrieved_at=_now(),
                detail={"filename": config.get("filename")},
            ),
        )

    def watch_plan(self, config: dict) -> WatchPlan:
        return WatchPlan(
            mode=WATCH_NONE,
            reason="An uploaded file cannot change on its own; upload again to update it.",
        )
