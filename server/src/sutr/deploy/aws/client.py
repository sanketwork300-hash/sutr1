"""A minimal signed HTTP client for the four AWS services this provider uses.

Two request shapes cover everything here:

- **JSON RPC** (ECR, CodeBuild, App Runner, CloudWatch Logs) - POST to the
  service root with an ``X-Amz-Target`` header naming the operation. The only
  difference between them is the JSON protocol version in the content type.
- **REST** (S3) - the verb and the path carry the meaning.

Error handling exists to turn AWS's error envelope into a sentence. An
``__type`` of ``RepositoryAlreadyExistsException`` is a fact the caller wants
to branch on, so the exception carries the bare code as well as the message.
"""

import json
from typing import Any

import httpx

from sutr.deploy.aws.sigv4 import sign_request
from sutr.deploy.base import ProviderError

DEFAULT_TIMEOUT_SECONDS = 60
UPLOAD_TIMEOUT_SECONDS = 300

# X-Amz-Target prefix and JSON protocol version per service.
_JSON_SERVICES = {
    "ecr": ("AmazonEC2ContainerRegistry_V20150921", "1.1"),
    "codebuild": ("CodeBuild_20161006", "1.1"),
    "apprunner": ("AppRunner", "1.0"),
    "logs": ("Logs_20140328", "1.1"),
}


class AwsError(ProviderError):
    """An AWS API refusal, with the service's own error code preserved."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AwsClient:
    """Signs and sends requests for one set of role credentials in one region."""

    def __init__(
        self,
        *,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        session_token: str = "",
    ) -> None:
        self.region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token

    def _endpoint(self, service: str) -> str:
        return f"https://{service}.{self.region}.amazonaws.com"

    async def _send(
        self,
        *,
        method: str,
        url: str,
        service: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> httpx.Response:
        signed = sign_request(
            method=method,
            url=url,
            region=self.region,
            service=service,
            access_key_id=self._access_key_id,
            secret_access_key=self._secret_access_key,
            session_token=self._session_token,
            headers=headers,
            payload=payload,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                return await client.request(method, url, content=payload, headers=signed)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach AWS {service}: {exc}")

    async def call(
        self,
        service: str,
        operation: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """One JSON-RPC operation, e.g. ``call("ecr", "CreateRepository", {...})``."""
        prefix, protocol = _JSON_SERVICES[service]
        payload = json.dumps(body or {}).encode("utf-8")
        response = await self._send(
            method="POST",
            url=self._endpoint(service),
            service=service,
            headers={
                "Content-Type": f"application/x-amz-json-{protocol}",
                "X-Amz-Target": f"{prefix}.{operation}",
            },
            payload=payload,
            timeout=timeout,
        )
        if response.is_success:
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {}
        raise self._error(response, f"{service}:{operation}")

    def _error(self, response: httpx.Response, what: str) -> AwsError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raw_type = payload.get("__type") or payload.get("code") or ""
        # AWS prefixes some error types with a namespace: "com.amazon...#Name".
        code = str(raw_type).split("#")[-1] or f"HTTP{response.status_code}"
        message = (
            payload.get("message")
            or payload.get("Message")
            or response.text[:300]
            or f"HTTP {response.status_code}"
        )
        if response.status_code in (401, 403):
            return AwsError(
                code,
                f"AWS refused {what} ({code}). The Identity Center role may lack "
                f"permission for this operation. {message}",
            )
        return AwsError(code, f"AWS {what} failed ({code}): {message}")

    # ── S3 ───────────────────────────────────────────────────────────────────

    def s3_url(self, bucket: str, key: str = "") -> str:
        # Path-style addressing avoids a DNS propagation wait on a bucket
        # created seconds earlier, which virtual-host style is prone to.
        base = f"https://s3.{self.region}.amazonaws.com/{bucket}"
        return f"{base}/{key}" if key else base

    async def s3_head_bucket(self, bucket: str) -> bool:
        response = await self._send(
            method="HEAD",
            url=self.s3_url(bucket),
            service="s3",
            headers={},
            payload=b"",
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            return False
        if response.is_success:
            return True
        raise self._error(response, "s3:HeadBucket")

    async def s3_create_bucket(self, bucket: str) -> None:
        # us-east-1 is the one region that must NOT be named in the
        # constraint; sending it there is an error.
        body = b""
        if self.region != "us-east-1":
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<LocationConstraint>{self.region}</LocationConstraint>"
                "</CreateBucketConfiguration>"
            ).encode("utf-8")
        response = await self._send(
            method="PUT",
            url=self.s3_url(bucket),
            service="s3",
            headers={"Content-Type": "application/xml"} if body else {},
            payload=body,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        if response.is_success or response.status_code == 409:
            # 409 is BucketAlreadyOwnedByYou, which is the state we wanted.
            return
        raise self._error(response, "s3:CreateBucket")

    async def s3_put_object(self, bucket: str, key: str, data: bytes) -> None:
        response = await self._send(
            method="PUT",
            url=self.s3_url(bucket, key),
            service="s3",
            headers={"Content-Type": "application/zip"},
            payload=data,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        if not response.is_success:
            raise self._error(response, "s3:PutObject")

    async def s3_delete_object(self, bucket: str, key: str) -> None:
        await self._send(
            method="DELETE",
            url=self.s3_url(bucket, key),
            service="s3",
            headers={},
            payload=b"",
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
