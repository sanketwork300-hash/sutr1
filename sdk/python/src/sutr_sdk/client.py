"""Sync and async Sutr clients.

Both delegate every decision to `_core`; they differ only in transport, so a
behaviour change lands in both at once.
"""

import asyncio
import os
import time
from typing import Any

import httpx

from sutr_sdk import _core
from sutr_sdk.errors import (
    ApprovalPending,
    ApprovalRequired,
    RateLimited,
    SutrConnectionError,
    SutrError,
)
from sutr_sdk.models import ApprovalDecision, Integration, Tool, ToolResult


class _BaseClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        access_token: str | None = None,
        timeout: float = _core.DEFAULT_TIMEOUT,
        max_retries: int = 2,
    ) -> None:
        # Environment credentials are a fallback for when the caller supplied
        # none — never an override. Letting SUTR_API_KEY win over an explicit
        # access_token would silently send the wrong credential (and a key
        # cannot read user-scoped endpoints such as /api/logs).
        if api_key is None and access_token is None:
            api_key = os.environ.get("SUTR_API_KEY")
        if not api_key and not access_token:
            raise SutrError(
                "No credentials. Pass api_key=... or set SUTR_API_KEY "
                "(create a key in the Sutr UI under Develop → API Keys)."
            )
        self._base_url = _core.normalize_base_url(
            base_url or os.environ.get("SUTR_BASE_URL") or _core.DEFAULT_BASE_URL
        )
        self._headers = _core.build_headers(api_key, access_token)
        self._timeout = timeout
        self._max_retries = max(0, max_retries)

    @property
    def base_url(self) -> str:
        return self._base_url

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return {"detail": response.text[:2000]}

    @staticmethod
    def _tools(payload: Any, integration_id: str | None) -> list[Tool]:
        rows = payload if isinstance(payload, list) else []
        tools = [Tool.from_dict(row) for row in rows if isinstance(row, dict)]
        if integration_id:
            for tool in tools:
                tool.integration_id = tool.integration_id or integration_id
        return tools


class Sutr(_BaseClient):
    """Synchronous client.

    with Sutr(api_key="ap_...") as sutr:
        result = sutr.call_tool("posthog", "list_projects", {})
        print(result.text)
    """

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key, **kwargs)
        self._http = httpx.Client(timeout=self._timeout, headers=self._headers)

    def __enter__(self) -> "Sutr":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _send(self, prepared: _core.Prepared, *, context: dict | None = None) -> Any:
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    prepared.method,
                    self._url(prepared.path),
                    json=prepared.json,
                    params=prepared.params,
                    timeout=prepared.timeout or self._timeout,
                )
            except httpx.HTTPError as exc:
                raise SutrConnectionError(f"Could not reach {self._base_url}: {exc}") from exc

            if (
                prepared.retryable
                and response.status_code in _core.RETRYABLE_STATUS
                and attempt < self._max_retries
            ):
                retry_after = response.headers.get("retry-after")
                delay = _core.retry_delay(
                    attempt, float(retry_after) if _is_number(retry_after) else None
                )
                attempt += 1
                time.sleep(delay)
                continue

            return _core.interpret(
                status_code=response.status_code,
                body=self._parse(response),
                headers=dict(response.headers),
                context=context,
            )

    # ── Tools ────────────────────────────────────────────────────────────────

    def list_tools(self, integration_id: str | None = None) -> list[Tool]:
        """Tools from one installed integration, or all of them."""
        return self._tools(self._send(_core.list_tools_request(integration_id)), integration_id)

    def call_tool(
        self,
        integration_id: str,
        tool_name: str,
        args: dict | None = None,
        *,
        additional_info: str | None = None,
        wait_for_approval: bool = False,
        approval_timeout: float | None = None,
    ) -> ToolResult:
        """Execute a tool.

        Raises `ApprovalRequired` when a human must decide first, or `ToolDenied`
        when policy blocks the tool outright. With `wait_for_approval=True` the
        call blocks until a human decides (up to `approval_timeout` seconds, or
        indefinitely when None) and then retries once — this is the same flow the
        `sutr tools call --wait` CLI command uses.

        `additional_info` is shown to the human reviewer; use it to explain why
        the agent wants the call.
        """
        prepared = _core.call_tool_request(integration_id, tool_name, args, additional_info)
        context = {"integration_id": integration_id, "tool_name": tool_name}
        try:
            return ToolResult.from_dict(self._send(prepared, context=context) or {})
        except ApprovalRequired as exc:
            if not wait_for_approval:
                raise
            decision = self.await_approval(exc.approval_request_id, timeout=approval_timeout)
            if not decision.approved:
                if decision.pending:
                    raise ApprovalPending(
                        f"Still waiting on a human decision: {exc.approval_url}",
                        approval_url=exc.approval_url,
                        approval_request_id=exc.approval_request_id,
                    ) from exc
                raise SutrError(decision.message or f"Approval {decision.status}") from exc
            # Approved: the grant is consumed by re-issuing the identical call.
            return ToolResult.from_dict(self._send(prepared, context=context) or {})

    def await_approval(
        self, approval_request_id: str, *, timeout: float | None = None
    ) -> ApprovalDecision:
        """Block until a human decides, or `timeout` seconds elapse.

        The server's long poll returns "pending" periodically; this loops across
        those windows so a caller sees one decision, not a stream of timeouts.
        """
        deadline = time.monotonic() + timeout if timeout else None
        last: ApprovalDecision | None = None
        while True:
            window = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return last or self.get_approval(approval_request_id)
                window = max(1, int(remaining))
            started = time.monotonic()
            decision = ApprovalDecision.from_dict(
                self._send(_core.await_approval_request(approval_request_id, window)) or {}
            )
            if not decision.pending:
                return decision
            last = decision
            if deadline is not None and time.monotonic() >= deadline:
                return decision
            delay = _core.poll_backoff(
                time.monotonic() - started,
                (deadline - time.monotonic()) if deadline is not None else None,
            )
            if delay > 0:
                time.sleep(delay)

    def get_approval(self, approval_request_id: str) -> ApprovalDecision:
        data = self._send(_core.approval_request_status_request(approval_request_id)) or {}
        # This endpoint returns the raw row (no "message"), so normalise shape.
        data.setdefault("approval_request_id", data.get("id", approval_request_id))
        data.setdefault("message", "")
        return ApprovalDecision.from_dict(data)

    # ── Integrations ─────────────────────────────────────────────────────────

    def integrations(self) -> list[Integration]:
        """The catalog, annotated with what this org has installed."""
        catalog = self._send(_core.integrations_request()) or []
        installed = {
            row.get("integration_id"): row
            for row in (self._send(_core.installed_request()) or [])
            if isinstance(row, dict)
        }
        return [
            Integration.from_dict(row, installed=installed.get(row.get("id")))
            for row in catalog
            if isinstance(row, dict)
        ]

    def installed(self) -> list[dict[str, Any]]:
        return self._send(_core.installed_request()) or []

    # ── Observability (raw JSON — see the API reference for shapes) ───────────

    def logs(
        self,
        *,
        integration: str | None = None,
        tool: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Org-wide tool-call log.

        Requires a **user** credential (`access_token=...`), not an API key:
        these rows carry every caller's tool arguments and results, so the
        server keeps them behind a human session deliberately. With an API key
        this raises `AuthenticationError`; use `usage()`/`usage_events()`, which
        are metering aggregates and are readable with a key.
        """
        return (
            self._send(
                _core.logs_request(
                    {
                        "integration": integration,
                        "tool": tool,
                        "outcome": outcome,
                        "limit": limit,
                        "offset": offset,
                    }
                )
            )
            or []
        )

    def usage(self, *, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        return self._send(_core.usage_summary_request({"start": start, "end": end})) or {}

    def usage_events(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return (
            self._send(
                _core.usage_events_request(
                    {"start": start, "end": end, "kind": kind, "limit": limit}
                )
            )
            or []
        )

    def deployments(self) -> list[dict[str, Any]]:
        return self._send(_core.deployments_request()) or []


class AsyncSutr(_BaseClient):
    """Asynchronous client — same surface as `Sutr`, awaited.

    async with AsyncSutr(api_key="ap_...") as sutr:
        result = await sutr.call_tool("posthog", "list_projects", {})
    """

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key, **kwargs)
        self._http = httpx.AsyncClient(timeout=self._timeout, headers=self._headers)

    async def __aenter__(self) -> "AsyncSutr":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, prepared: _core.Prepared, *, context: dict | None = None) -> Any:
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    prepared.method,
                    self._url(prepared.path),
                    json=prepared.json,
                    params=prepared.params,
                    timeout=prepared.timeout or self._timeout,
                )
            except httpx.HTTPError as exc:
                raise SutrConnectionError(f"Could not reach {self._base_url}: {exc}") from exc

            if (
                prepared.retryable
                and response.status_code in _core.RETRYABLE_STATUS
                and attempt < self._max_retries
            ):
                retry_after = response.headers.get("retry-after")
                delay = _core.retry_delay(
                    attempt, float(retry_after) if _is_number(retry_after) else None
                )
                attempt += 1
                await asyncio.sleep(delay)
                continue

            return _core.interpret(
                status_code=response.status_code,
                body=self._parse(response),
                headers=dict(response.headers),
                context=context,
            )

    async def list_tools(self, integration_id: str | None = None) -> list[Tool]:
        payload = await self._send(_core.list_tools_request(integration_id))
        return self._tools(payload, integration_id)

    async def call_tool(
        self,
        integration_id: str,
        tool_name: str,
        args: dict | None = None,
        *,
        additional_info: str | None = None,
        wait_for_approval: bool = False,
        approval_timeout: float | None = None,
    ) -> ToolResult:
        prepared = _core.call_tool_request(integration_id, tool_name, args, additional_info)
        context = {"integration_id": integration_id, "tool_name": tool_name}
        try:
            return ToolResult.from_dict(await self._send(prepared, context=context) or {})
        except ApprovalRequired as exc:
            if not wait_for_approval:
                raise
            decision = await self.await_approval(exc.approval_request_id, timeout=approval_timeout)
            if not decision.approved:
                if decision.pending:
                    raise ApprovalPending(
                        f"Still waiting on a human decision: {exc.approval_url}",
                        approval_url=exc.approval_url,
                        approval_request_id=exc.approval_request_id,
                    ) from exc
                raise SutrError(decision.message or f"Approval {decision.status}") from exc
            return ToolResult.from_dict(await self._send(prepared, context=context) or {})

    async def await_approval(
        self, approval_request_id: str, *, timeout: float | None = None
    ) -> ApprovalDecision:
        deadline = time.monotonic() + timeout if timeout else None
        last: ApprovalDecision | None = None
        while True:
            window = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return last or await self.get_approval(approval_request_id)
                window = max(1, int(remaining))
            started = time.monotonic()
            decision = ApprovalDecision.from_dict(
                await self._send(_core.await_approval_request(approval_request_id, window)) or {}
            )
            if not decision.pending:
                return decision
            last = decision
            if deadline is not None and time.monotonic() >= deadline:
                return decision
            delay = _core.poll_backoff(
                time.monotonic() - started,
                (deadline - time.monotonic()) if deadline is not None else None,
            )
            if delay > 0:
                await asyncio.sleep(delay)

    async def get_approval(self, approval_request_id: str) -> ApprovalDecision:
        data = await self._send(_core.approval_request_status_request(approval_request_id)) or {}
        data.setdefault("approval_request_id", data.get("id", approval_request_id))
        data.setdefault("message", "")
        return ApprovalDecision.from_dict(data)

    async def integrations(self) -> list[Integration]:
        catalog = await self._send(_core.integrations_request()) or []
        installed_rows = await self._send(_core.installed_request()) or []
        installed = {
            row.get("integration_id"): row for row in installed_rows if isinstance(row, dict)
        }
        return [
            Integration.from_dict(row, installed=installed.get(row.get("id")))
            for row in catalog
            if isinstance(row, dict)
        ]

    async def installed(self) -> list[dict[str, Any]]:
        return await self._send(_core.installed_request()) or []

    async def logs(
        self,
        *,
        integration: str | None = None,
        tool: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Org-wide tool-call log. Requires a user credential, not an API key —
        see `Sutr.logs` for why."""
        return (
            await self._send(
                _core.logs_request(
                    {
                        "integration": integration,
                        "tool": tool,
                        "outcome": outcome,
                        "limit": limit,
                        "offset": offset,
                    }
                )
            )
            or []
        )

    async def usage(self, *, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        return await self._send(_core.usage_summary_request({"start": start, "end": end})) or {}

    async def usage_events(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return (
            await self._send(
                _core.usage_events_request(
                    {"start": start, "end": end, "kind": kind, "limit": limit}
                )
            )
            or []
        )

    async def deployments(self) -> list[dict[str, Any]]:
        return await self._send(_core.deployments_request()) or []


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


# Retryable status codes are shared with RateLimited handling above.
__all__ = ["Sutr", "AsyncSutr", "RateLimited"]
