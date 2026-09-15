"""A plain OpenAPI URL, and a Swagger UI page.

The URL connector is the one that makes continuous sync cheap: it sends the
`ETag` and `Last-Modified` it was given back as `If-None-Match` and
`If-Modified-Since`, so a poll that finds nothing costs a 304 and no body.
Polling without that is how a well-meaning integration becomes a nuisance to
the API it is watching.

The Swagger UI connector exists because the LLD (§3.3) calls it out directly:
*"swagger-ui → … automatically — many teams only know the Swagger UI URL"*.
Someone who has only ever seen `/swagger-ui/index.html` should not have to go
and find the JSON themselves.
"""

import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from sutr.openapi.errors import OpenAPIError
from sutr.openapi.limits import MAX_SPEC_BYTES, URL_FETCH_TIMEOUT_SECONDS
from sutr.openapi.loader import fetch_spec_from_url
from sutr.source_connectors.base import (
    WATCH_POLL,
    ConfigField,
    ConnectionResult,
    ConnectorError,
    Discovered,
    FetchResult,
    Provenance,
    SourceConnector,
    WatchPlan,
)
from sutr.upstream_safety import UnsafeUpstreamUrlError, validate_safe_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UrlConnector(SourceConnector):
    id = "url"
    display_name = "OpenAPI URL"
    description = "A direct link to an OpenAPI or Swagger document."
    supports_watch = True
    config_fields = (
        ConfigField(
            key="url",
            label="Specification URL",
            kind="url",
            placeholder="https://api.example.com/openapi.json",
        ),
    )

    async def connect(self, config: dict, secrets: dict) -> ConnectionResult:
        url = (config.get("url") or "").strip()
        if not url:
            return ConnectionResult(connected=False, message="A URL is required.")
        try:
            validate_safe_url(url)
        except UnsafeUpstreamUrlError as exc:
            return ConnectionResult(connected=False, message=f"Unsafe URL: {exc}")
        try:
            result = await self.fetch(config, secrets)
        except (ConnectorError, OpenAPIError) as exc:
            return ConnectionResult(connected=False, message=getattr(exc, "message", str(exc)))
        validation = self.validate(result.content or "")
        if not validation.valid:
            return ConnectionResult(
                connected=False,
                message=(
                    f"The URL was reachable but the document is not usable: {validation.message}"
                ),
            )
        return ConnectionResult(
            connected=True,
            message=f"Found {validation.api_title} {validation.api_version}.",
            detail={"operation_count": validation.operation_count},
        )

    async def fetch(self, config: dict, secrets: dict, *, known: Provenance | None = None):
        url = (config.get("url") or "").strip()
        if not url:
            raise ConnectorError("no_url", "This source has no URL configured.")
        return await conditional_fetch(url, source_type=self.id, known=known)

    def watch_plan(self, config: dict) -> WatchPlan:
        return WatchPlan(
            mode=WATCH_POLL,
            interval_seconds=3600,
            # Whether the server actually honours it is only known after the
            # first fetch; the plan says what we will attempt.
            conditional=True,
            reason="Polled with If-None-Match / If-Modified-Since, so an unchanged "
            "document costs a 304 and no body.",
        )


async def conditional_fetch(
    url: str, *, source_type: str, known: Provenance | None = None, headers: dict | None = None
) -> FetchResult:
    """GET a document, asking the server not to resend what we already have.

    Shares `fetch_spec_from_url`'s safety rules by reusing its validation and
    limits: SSRF-screened, redirects refused (a redirect could bounce a
    credential to another host), and size-capped.
    """
    try:
        validate_safe_url(url)
    except UnsafeUpstreamUrlError as exc:
        raise ConnectorError("unsafe_url", f"Unsafe specification URL: {exc}")

    request_headers = dict(headers or {})
    if known:
        if known.etag:
            request_headers["If-None-Match"] = known.etag
        if known.last_modified:
            request_headers["If-Modified-Since"] = known.last_modified

    try:
        async with httpx.AsyncClient(
            timeout=URL_FETCH_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            async with client.stream("GET", url, headers=request_headers) as response:
                if response.status_code == 304:
                    # The source says we already have this. Carry the known
                    # provenance forward, with a fresh retrieval time.
                    provenance = known or Provenance(source_type=source_type, source_uri=url)
                    provenance.retrieved_at = _now()
                    return FetchResult(content=None, provenance=provenance, not_modified=True)
                if 300 <= response.status_code < 400:
                    raise ConnectorError(
                        "fetch_redirect",
                        "The URL redirected; redirects are not followed. Use the final URL.",
                    )
                if response.status_code >= 400:
                    raise ConnectorError(
                        "fetch_failed", f"The URL returned HTTP {response.status_code}."
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_SPEC_BYTES:
                        raise ConnectorError(
                            "spec_too_large", "The document exceeds the maximum allowed size."
                        )
                    chunks.append(chunk)
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
    except httpx.HTTPError as exc:
        raise ConnectorError("fetch_failed", f"Could not fetch {url}: {exc}")

    content = b"".join(chunks).decode("utf-8", errors="replace")
    return FetchResult(
        content=content,
        provenance=Provenance(
            source_type=source_type,
            source_uri=url,
            source_version=etag or last_modified,
            etag=etag,
            last_modified=last_modified,
            retrieved_at=_now(),
        ),
    )


# ── Swagger UI ───────────────────────────────────────────────────────────────

# Swagger UI is configured by JavaScript, so the specification URL is found in
# the page's own initializer rather than in markup. These are the two shapes
# every version emits: `url: "..."` and `urls: [{url: "...", name: "..."}]`.
_URL_RE = re.compile(r"""["']?url["']?\s*:\s*["']([^"']+)["']""")
_SPEC_RE = re.compile(r"""["']?spec["']?\s*:\s*\{""")

# Tried in order when the page yields nothing — the conventional places a
# Swagger UI deployment puts its document.
_CONVENTIONAL_PATHS = (
    "swagger.json",
    "openapi.json",
    "v2/api-docs",
    "v3/api-docs",
    "api-docs",
    "swagger.yaml",
    "openapi.yaml",
)

_INITIALIZER_PATHS = ("swagger-initializer.js", "swagger-ui-init.js")

# Anything that is obviously not a specification.
_IGNORED_URL_SUFFIXES = (".css", ".png", ".ico", ".js", ".map", ".html")


class SwaggerUiConnector(SourceConnector):
    id = "swagger_ui"
    display_name = "Swagger UI page"
    description = (
        "A Swagger UI URL. The specification behind the page is located automatically — "
        "many teams only ever see the UI."
    )
    supports_watch = True
    supports_discovery = True
    config_fields = (
        ConfigField(
            key="url",
            label="Swagger UI URL",
            kind="url",
            placeholder="https://api.example.com/swagger-ui/index.html",
        ),
        ConfigField(
            key="spec_url",
            label="Specification URL",
            required=False,
            help="Filled in automatically once discovery finds it.",
        ),
    )

    async def connect(self, config: dict, secrets: dict) -> ConnectionResult:
        page_url = (config.get("url") or "").strip()
        if not page_url:
            return ConnectionResult(connected=False, message="A Swagger UI URL is required.")
        try:
            candidates = await self.discover(config, secrets)
        except ConnectorError as exc:
            return ConnectionResult(connected=False, message=exc.message)
        if not candidates:
            return ConnectionResult(
                connected=False,
                message=(
                    "No specification could be found behind that page. Point at the "
                    "document itself with the OpenAPI URL source instead."
                ),
            )
        return ConnectionResult(
            connected=True,
            message=f"Found {candidates[0].label}.",
            detail={"spec_url": candidates[0].identifier},
        )

    async def discover(self, config: dict, secrets: dict) -> list[Discovered]:
        page_url = (config.get("url") or "").strip()
        if not page_url:
            return []
        found: list[str] = []
        for candidate in await self._candidate_urls(page_url):
            if candidate in found:
                continue
            if await self._looks_like_a_specification(candidate):
                found.append(candidate)
        return [
            Discovered(identifier=url, label=url, detail={"discovered_from": page_url})
            for url in found
        ]

    async def fetch(self, config: dict, secrets: dict, *, known: Provenance | None = None):
        spec_url = (config.get("spec_url") or "").strip()
        if not spec_url:
            candidates = await self.discover(config, secrets)
            if not candidates:
                raise ConnectorError(
                    "no_spec_found",
                    "No specification could be found behind that Swagger UI page.",
                )
            spec_url = candidates[0].identifier
        result = await conditional_fetch(spec_url, source_type=self.id, known=known)
        result.provenance.detail["page_url"] = config.get("url")
        result.provenance.detail["spec_url"] = spec_url
        return result

    def watch_plan(self, config: dict) -> WatchPlan:
        return WatchPlan(
            mode=WATCH_POLL,
            interval_seconds=3600,
            conditional=True,
            reason="The discovered specification URL is polled conditionally.",
        )

    # ── discovery internals ─────────────────────────────────────────────────

    async def _candidate_urls(self, page_url: str) -> list[str]:
        """Specification URLs a Swagger UI page might be pointing at."""
        candidates: list[str] = []
        page = await self._get_text(page_url)
        if page:
            candidates += self._urls_in(page, page_url)
            # Modern Swagger UI keeps its configuration in a separate
            # initializer script rather than inline in the page.
            for initializer in _INITIALIZER_PATHS:
                script = await self._get_text(urljoin(page_url, initializer))
                if script:
                    candidates += self._urls_in(script, page_url)

        base = page_url.rsplit("/", 1)[0] + "/"
        parent = urljoin(base, "../")
        for path in _CONVENTIONAL_PATHS:
            candidates.append(urljoin(base, path))
            candidates.append(urljoin(parent, path))
        return candidates

    def _urls_in(self, text: str, page_url: str) -> list[str]:
        urls = []
        for match in _URL_RE.finditer(text):
            value = match.group(1).strip()
            if not value or value.startswith(("data:", "#")):
                continue
            if value.lower().endswith(_IGNORED_URL_SUFFIXES):
                continue
            urls.append(urljoin(page_url, value))
        if _SPEC_RE.search(text):
            # The page embeds the document inline rather than fetching it.
            # Nothing to follow, and nothing to poll — say so where the user
            # will see it rather than silently finding nothing.
            raise ConnectorError(
                "inline_spec",
                "That page embeds its specification inline rather than serving it at a URL. "
                "Copy the document and use the paste source.",
            )
        return urls

    async def _get_text(self, url: str) -> str | None:
        try:
            return await fetch_spec_from_url(url)
        except OpenAPIError:
            return None

    async def _looks_like_a_specification(self, url: str) -> bool:
        """Fetch a candidate and check it parses as a specification.

        Checked rather than assumed: a Swagger UI page references its own
        assets too, and guessing by filename would offer `swagger-ui.css` as an
        API definition.
        """
        if urlsplit(url).scheme not in ("http", "https"):
            return False
        text = await self._get_text(url)
        if not text:
            return False
        return self.validate(text).valid
