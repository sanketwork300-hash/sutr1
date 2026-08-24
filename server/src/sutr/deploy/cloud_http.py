"""Shared plumbing for the cloud deployment providers.

Three things every one of them needs and none of them should reimplement: a
JSON request that turns a cloud's error envelope into one readable sentence,
a poll loop with a deadline, and the zip -> tar.gz conversion Azure's build
service insists on.

Deliberately not SSRF-screened: these URLs are hard-coded cloud API hosts, not
user input. The user-controlled parts (project ids, registry names) are path
segments, and every caller quotes them.
"""

import asyncio
import gzip
import io
import tarfile
import time
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from sutr.deploy.base import ProviderError

DEFAULT_TIMEOUT_SECONDS = 60
# Uploads and image builds are the slow parts; give them their own budget.
UPLOAD_TIMEOUT_SECONDS = 300


def _error_message(payload: Any, status: int) -> str:
    """Pull the human part out of whichever error envelope this cloud uses."""
    if isinstance(payload, dict):
        # Google: {"error": {"message": ...}}; Azure: {"error": {"message": ...}}
        # or {"message": ...}; AWS JSON protocols: {"message"|"Message": ...}.
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if message:
                return str(message)
        if isinstance(error, str):
            return error
        for key in ("message", "Message", "error_description", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return f"HTTP {status}"


async def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    token: str | None = None,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    # Statuses the caller expects and wants to handle itself (404 on an
    # existence check, 409 on a create-if-absent). Returned as None instead of
    # raising, so "already there" never reads as a failure.
    tolerate: tuple[int, ...] = (),
) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(
                method, url, json=json_body, params=params, headers=request_headers
            )
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not reach {provider}: {exc}")

    if response.status_code in tolerate:
        return None
    if response.status_code in (401, 403):
        detail = _error_message(_safe_json(response), response.status_code)
        raise ProviderError(
            f"{provider} refused the request ({response.status_code}). The "
            f"authorization may have expired, or the account may lack permission "
            f"for this operation. Details: {detail}"
        )
    if not response.is_success:
        raise ProviderError(
            f"{provider} returned {response.status_code}: "
            f"{_error_message(_safe_json(response), response.status_code)}"
        )
    if not response.content:
        return {}
    return _safe_json(response)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"message": response.text[:500]}


async def put_bytes(
    url: str,
    data: bytes,
    *,
    provider: str,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    method: str = "PUT",
    timeout: float = UPLOAD_TIMEOUT_SECONDS,
) -> None:
    """Upload a blob. Used for source archives, which are not JSON."""
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(method, url, content=data, headers=request_headers)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not upload the package to {provider}: {exc}")
    if not response.is_success:
        raise ProviderError(
            f"{provider} rejected the package upload ({response.status_code}): "
            f"{response.text[:300]}"
        )


async def poll_until(
    fetch: Callable[[], Awaitable[Any]],
    done: Callable[[Any], bool],
    *,
    provider: str,
    what: str,
    timeout_seconds: float,
    interval_seconds: float = 5.0,
) -> Any:
    """Poll `fetch` until `done`, or give up with a message naming what stalled.

    A timeout here is not the same as a failure: the build or rollout may well
    still finish. The message says so, because telling someone their deploy
    failed when it is merely slow sends them to undo work that is succeeding.
    """
    deadline = time.monotonic() + timeout_seconds
    latest = None
    while time.monotonic() < deadline:
        latest = await fetch()
        if done(latest):
            return latest
        await asyncio.sleep(interval_seconds)
    raise ProviderError(
        f"{provider} did not finish {what} within {int(timeout_seconds)}s. It may "
        f"still complete — check the deployment again shortly, or look in the "
        f"provider's console."
    )


def zip_to_tar_gz(zip_bytes: bytes) -> bytes:
    """Repack the generated package as a gzipped tar.

    Azure Container Registry builds accept a tar.gz source archive and nothing
    else, while every other consumer of the package (download, Docker, Cloud
    Build, CodeBuild) wants the zip. Converting here keeps one canonical
    artifact on the deployment row instead of storing the same files twice.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        raw_tar = io.BytesIO()
        with tarfile.open(fileobj=raw_tar, mode="w") as tar:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                data = archive.read(info.filename)
                entry = tarfile.TarInfo(name=info.filename)
                entry.size = len(data)
                # Fixed mtime keeps the archive deterministic, matching the
                # zip generator's deliberate reproducibility.
                entry.mtime = 0
                entry.mode = 0o644
                tar.addfile(entry, io.BytesIO(data))
        out.write(gzip.compress(raw_tar.getvalue(), mtime=0))
    return out.getvalue()
