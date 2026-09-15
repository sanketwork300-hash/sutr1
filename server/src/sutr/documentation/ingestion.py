"""Taking a document in.

The narrow front door for every document that enters the platform, whether it
arrives as an upload, as bytes fetched from a URI, or from a test. Everything
after this point — parsing, extraction, retrieval — assumes the invariants
established here:

- the tenant is known, and it is on the row;
- the bytes are within the configured ceiling;
- the format was determined from the *content*, not from a filename someone
  can type anything into;
- identical content uploaded twice is one document, not two.

Fetching a document from a URL goes through the same SSRF screen as every other
outbound fetch (`upstream_safety`). A documentation URI is user-supplied input
pointed at the server's own network, and there is no reason to trust it more
than an OpenAPI source URL.
"""

import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from sqlmodel import Session, select

from sutr.common.errors import InvalidRequestError
from sutr.config import settings
from sutr.documentation.parsing import detect_kind
from sutr.documentation.storage import digest, get_storage
from sutr.models.document import KIND_TEXT, KINDS, STATUS_UPLOADED, Document
from sutr.models.document_job import STATUS_QUEUED, DocumentJob
from sutr.upstream_safety import UnsafeUpstreamUrlError, validate_safe_url

FETCH_TIMEOUT = 30.0


@dataclass
class Ingested:
    document: Document
    job: DocumentJob
    deduplicated: bool = False


def ingest_bytes(
    session: Session,
    *,
    org_id: uuid.UUID,
    data: bytes,
    filename: str = "",
    media_type: str = "",
    source_uri: str = "",
    project_id: uuid.UUID | None = None,
    declared_kind: str | None = None,
) -> Ingested:
    """Store `data` as a document and queue a job to process it."""
    if not data:
        raise InvalidRequestError("The document is empty.")
    if len(data) > settings.document_max_bytes:
        raise InvalidRequestError(
            f"The document is {len(data)} bytes; the limit is {settings.document_max_bytes} bytes."
        )

    # Content wins over the declared type. A `.txt` full of `%PDF-1.4` is a PDF,
    # and handing it to the text parser produces a chunk of binary noise that
    # then gets extracted from as though it were prose.
    kind = detect_kind(filename=filename, media_type=media_type, content=data)
    if kind == KIND_TEXT and declared_kind in KINDS:
        kind = declared_kind

    sha = digest(data)
    existing = session.exec(
        select(Document).where(Document.org_id == org_id, Document.sha256 == sha)
    ).first()
    if existing is not None:
        # Same bytes, same tenant: re-processing is a new job over the existing
        # document, so citations from earlier runs keep pointing somewhere.
        job = _queue(session, existing)
        return Ingested(document=existing, job=job, deduplicated=True)

    _, inline = get_storage().put(data)
    document = Document(
        org_id=org_id,
        project_id=project_id,
        filename=filename or f"document-{sha[:8]}",
        media_type=media_type,
        kind=kind,
        source_uri=source_uri,
        size_bytes=len(data),
        sha256=sha,
        content=inline or b"",
        status=STATUS_UPLOADED,
    )
    session.add(document)
    session.flush()
    job = _queue(session, document)
    return Ingested(document=document, job=job)


async def ingest_uri(
    session: Session,
    *,
    org_id: uuid.UUID,
    uri: str,
    project_id: uuid.UUID | None = None,
    declared_kind: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> Ingested:
    """Fetch `uri` and ingest what comes back.

    This is the LLD's `document_uri` input. It is screened for SSRF before the
    request is made, and the response is size-capped while streaming rather
    than after: a server that answers with ten gigabytes should cost us the
    first twenty-five megabytes, not all of it.
    """
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https"):
        raise InvalidRequestError(
            f"Only http and https documentation URIs are supported, not {parsed.scheme or 'none'}."
        )
    try:
        validate_safe_url(uri)
    except UnsafeUpstreamUrlError as exc:
        raise InvalidRequestError(f"The documentation URI was refused: {exc}") from exc

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False)
    try:
        chunks: list[bytes] = []
        size = 0
        async with client.stream("GET", uri) as response:
            if response.status_code >= 400:
                raise InvalidRequestError(
                    f"Fetching the document returned HTTP {response.status_code}."
                )
            async for piece in response.aiter_bytes():
                size += len(piece)
                if size > settings.document_max_bytes:
                    raise InvalidRequestError(
                        f"The document exceeds the {settings.document_max_bytes} byte limit."
                    )
                chunks.append(piece)
            media_type = response.headers.get("content-type", "").split(";")[0].strip()
    finally:
        if owns_client:
            await client.aclose()

    return ingest_bytes(
        session,
        org_id=org_id,
        data=b"".join(chunks),
        filename=_filename_from(uri),
        media_type=media_type,
        source_uri=uri,
        project_id=project_id,
        declared_kind=declared_kind,
    )


def _queue(session: Session, document: Document) -> DocumentJob:
    job = DocumentJob(org_id=document.org_id, document_id=document.id, status=STATUS_QUEUED)
    session.add(job)
    session.flush()
    return job


def _filename_from(uri: str) -> str:
    name = urlparse(uri).path.rsplit("/", 1)[-1]
    return name or "document"
