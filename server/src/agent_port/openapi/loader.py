"""Loading and syntactic parsing of OpenAPI documents (JSON or YAML)."""

import json

import httpx
import yaml

from agent_port.openapi.errors import OpenAPIError
from agent_port.openapi.limits import MAX_SPEC_BYTES, URL_FETCH_TIMEOUT_SECONDS
from agent_port.upstream_safety import UnsafeUpstreamUrlError, validate_safe_url


def parse_spec_text(text: str) -> dict:
    """Parse a raw OpenAPI document. Tries JSON first, then YAML (safe loader).

    Raises OpenAPIError on empty/oversized/malformed input or when the top
    level isn't an object.
    """
    if not text or not text.strip():
        raise OpenAPIError("empty_spec", "The specification is empty.")
    if len(text.encode("utf-8", errors="replace")) > MAX_SPEC_BYTES:
        raise OpenAPIError(
            "spec_too_large",
            f"The specification exceeds the maximum size of {MAX_SPEC_BYTES // (1024 * 1024)} MiB.",
        )

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise OpenAPIError(
                "parse_error", f"The document is neither valid JSON nor valid YAML: {exc}"
            )

    if not isinstance(data, dict):
        raise OpenAPIError("not_an_object", "The specification's top level must be an object.")
    return data


async def fetch_spec_from_url(url: str) -> str:
    """Fetch a spec over HTTP with SSRF screening and a size cap.

    The URL is untrusted user input: loopback/private/link-local targets are
    rejected, redirects are not followed (a redirect could bounce to an
    internal address), and the body is streamed with a hard byte limit.
    """
    try:
        validate_safe_url(url)
    except UnsafeUpstreamUrlError as exc:
        raise OpenAPIError("unsafe_url", f"Unsafe specification URL: {exc}")

    try:
        async with httpx.AsyncClient(
            timeout=URL_FETCH_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise OpenAPIError(
                        "fetch_failed",
                        f"The specification URL returned HTTP {response.status_code}.",
                    )
                if 300 <= response.status_code < 400:
                    raise OpenAPIError(
                        "fetch_redirect",
                        "The specification URL redirected; redirects are not followed. "
                        "Use the final URL directly.",
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_SPEC_BYTES:
                        raise OpenAPIError(
                            "spec_too_large",
                            "The fetched specification exceeds the maximum allowed size.",
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise OpenAPIError("fetch_failed", f"Could not fetch the specification URL: {exc}")

    return b"".join(chunks).decode("utf-8", errors="replace")
