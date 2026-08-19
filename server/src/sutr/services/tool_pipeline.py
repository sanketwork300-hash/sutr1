"""Canonical tool-execution pipeline — the single path every surface calls.

REST (`api/tools.py`) and the MCP gateway (`mcp/server.py`) used to carry
independent copies of this logic, and they had already drifted: REST wrote
gate logs with `outcome="approval_required"` (which the UI and the expiry
decoration in `api/logs.py` don't recognize), never recorded the requester IP
or user agent, and never set `access_reason="approved_once"` on consumed
approvals; MCP never recorded call duration. This module is now the only
implementation. Interface layers build a `CallContext`, run the two stages,
and translate the typed results into their own response shapes — they must
not re-implement any step.

Stages:
1. `evaluate_gate` — policy evaluation and approval gating, inside the
   caller's DB session. Emits the denied log / pending approval + gate log.
2. `execute_tool` — upstream dispatch with OAuth pre-flight and one
   auth-error retry, then the execution log write (or in-place update of the
   gate log) and analytics. Opens its own short-lived sessions via
   `db.engine` so it can be used from long-poll continuations.

Canonical log semantics (both surfaces, enforced here):
- gate log outcome is `pending` (legacy REST rows may say `approval_required`;
  readers stay tolerant, writers never produce it again)
- `result_json` is stored only on success
- every log carries args_hash, requester_ip, user_agent, api key metadata,
  impersonator attribution, and duration_ms for executed/error outcomes
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass

from sqlmodel import Session, col, select

from sutr import api_client, db
from sutr.analytics import posthog_client
from sutr.approvals.policy import evaluate_policy
from sutr.approvals.requests import (
    create_auto_approved_request,
    get_or_create_approval_request,
    try_consume_approved_request,
)
from sutr.config import settings
from sutr.integrations import registry as integration_registry
from sutr.integrations.types import CustomIntegration
from sutr.mcp import client as mcp_client
from sutr.mcp import oauth as oauth_refresh
from sutr.models.integration import InstalledIntegration
from sutr.models.log import LogEntry
from sutr.models.oauth import OAuthState

logger = logging.getLogger(__name__)

# Outcomes a gate log can be in before execution resolves it. `approved` is set
# by the human-decision endpoint; `approval_required` only exists on rows
# written by the pre-unification REST path.
GATE_LOG_OUTCOMES = ("pending", "approved", "approval_required")


@dataclass
class CallContext:
    """Who is calling, from where, and why — everything logging needs."""

    org_id: uuid.UUID
    source: str  # "api" | "mcp" — for analytics only, never for behavior
    api_key_id: uuid.UUID | None = None
    api_key_label: str | None = None
    api_key_prefix: str | None = None
    impersonator_user_id: uuid.UUID | None = None
    requester_ip: str | None = None
    user_agent: str | None = None
    additional_info: str | None = None

    @property
    def requested_by_agent(self) -> str | None:
        return f"api_key:{self.api_key_id}" if self.api_key_id else None

    @classmethod
    def from_agent_auth(
        cls,
        auth,
        *,
        source: str,
        requester_ip: str | None = None,
        user_agent: str | None = None,
        additional_info: str | None = None,
    ) -> "CallContext":
        return cls(
            org_id=auth.org.id,
            source=source,
            api_key_id=auth.api_key.id if auth.api_key else None,
            api_key_label=auth.api_key.name if auth.api_key else None,
            api_key_prefix=auth.api_key.key_prefix if auth.api_key else None,
            impersonator_user_id=auth.impersonator.id if auth.impersonator is not None else None,
            requester_ip=requester_ip,
            user_agent=user_agent,
            additional_info=additional_info,
        )


@dataclass
class GateResult:
    status: str  # "denied" | "approval_pending" | "ready"
    args_hash: str | None
    approval_request_id: uuid.UUID | None = None
    # approval_pending only:
    approval_url: str | None = None
    # ready only:
    access_reason: str | None = None  # "approved_once" | "approved_any" | None
    pending_log_id: int | None = None  # gate log to resolve in place at execution


@dataclass
class ExecutionOutcome:
    outcome: str  # "executed" | "error"
    result: dict
    error: str | None
    duration_ms: int
    # Pre-dispatch failure; when set, nothing was logged and `outcome` is "error":
    # "integration_not_found" | "tool_not_found"
    failure: str | None = None


def find_gate_log(session: Session, approval_request_id: uuid.UUID) -> LogEntry | None:
    """The unresolved log entry created when a call was gated on approval."""
    return session.exec(
        select(LogEntry)
        .where(LogEntry.approval_request_id == approval_request_id)
        .where(col(LogEntry.outcome).in_(GATE_LOG_OUTCOMES))
    ).first()


def evaluate_gate(
    session: Session,
    ctx: CallContext,
    integration_id: str,
    tool_name: str,
    args: dict,
) -> GateResult:
    """Run policy + approval gating. Writes the denied log or the pending
    approval request (+ its gate log); execution side effects happen in
    `execute_tool`."""
    decision = evaluate_policy(session, ctx.org_id, integration_id, tool_name, args)

    if not decision.allowed and decision.reason == "denied":
        session.add(
            LogEntry(
                org_id=ctx.org_id,
                integration_id=integration_id,
                tool_name=tool_name,
                args_json=json.dumps(args),
                args_hash=decision.args_hash,
                outcome="denied",
                requester_ip=ctx.requester_ip,
                user_agent=ctx.user_agent,
                api_key_label=ctx.api_key_label,
                api_key_prefix=ctx.api_key_prefix,
                impersonator_user_id=ctx.impersonator_user_id,
                additional_info=ctx.additional_info,
            )
        )
        session.commit()
        posthog_client.capture(
            distinct_id=str(ctx.org_id),
            event="tool_call_denied_by_policy",
            properties={
                "integration_id": integration_id,
                "tool_name": tool_name,
                "source": ctx.source,
            },
        )
        return GateResult(status="denied", args_hash=decision.args_hash)

    if not decision.allowed:
        # require_approval: consume an approve-once grant if one matches.
        consumed = try_consume_approved_request(
            session, ctx.org_id, integration_id, tool_name, decision.args_hash
        )
        if consumed is None:
            approval_req = get_or_create_approval_request(
                session,
                ctx.org_id,
                integration_id,
                tool_name,
                args,
                requested_by_agent=ctx.requested_by_agent,
                requester_ip=ctx.requester_ip,
                user_agent=ctx.user_agent,
                api_key_label=ctx.api_key_label,
                api_key_prefix=ctx.api_key_prefix,
                additional_info=ctx.additional_info,
            )
            # One gate log per approval request — the request may be reused
            # across agent retries, and the log must not duplicate.
            if not find_gate_log(session, approval_req.id):
                session.add(
                    LogEntry(
                        org_id=ctx.org_id,
                        integration_id=integration_id,
                        tool_name=tool_name,
                        args_json=json.dumps(args),
                        args_hash=decision.args_hash,
                        approval_request_id=approval_req.id,
                        outcome="pending",
                        requester_ip=ctx.requester_ip,
                        user_agent=ctx.user_agent,
                        api_key_label=ctx.api_key_label,
                        api_key_prefix=ctx.api_key_prefix,
                        impersonator_user_id=ctx.impersonator_user_id,
                        additional_info=ctx.additional_info,
                    )
                )
                session.commit()
            return GateResult(
                status="approval_pending",
                args_hash=decision.args_hash,
                approval_request_id=approval_req.id,
                approval_url=f"{settings.ui_base_url}/approve/{approval_req.id}",
            )

        gate_log = find_gate_log(session, consumed.id)
        return GateResult(
            status="ready",
            args_hash=decision.args_hash,
            access_reason="approved_once",
            approval_request_id=consumed.id,
            pending_log_id=gate_log.id if gate_log else None,
        )

    # tool_allowed: record an auto-approved request for the audit trail.
    auto_req = create_auto_approved_request(
        session,
        ctx.org_id,
        integration_id,
        tool_name,
        args,
        requested_by_agent=ctx.requested_by_agent,
        requester_ip=ctx.requester_ip,
        user_agent=ctx.user_agent,
        api_key_label=ctx.api_key_label,
        api_key_prefix=ctx.api_key_prefix,
        additional_info=ctx.additional_info,
    )
    return GateResult(
        status="ready",
        args_hash=decision.args_hash,
        access_reason="approved_any",
        approval_request_id=auto_req.id,
    )


async def _attempt_with_refresh(call, installed, oauth_state):
    """Run `call(oauth_state)` with one retry after an OAuth refresh when the
    first attempt fails with an auth error. Returns (result, error)."""
    result: dict = {}
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            result = await call(oauth_state)
            last_error = None
            break
        except Exception as e:
            last_error = e
            if (
                attempt == 0
                and installed.auth_method == "oauth"
                and oauth_state
                and oauth_refresh.is_auth_error(e)
            ):
                refreshed = await oauth_refresh.refresh_tokens(oauth_state)
                if refreshed:
                    oauth_state = refreshed
                    continue
            break
    return result, last_error


async def execute_tool(
    ctx: CallContext,
    integration_id: str,
    tool_name: str,
    arguments: dict,
    gate: GateResult,
) -> ExecutionOutcome:
    """Dispatch to the upstream tool and record the outcome.

    The caller must have obtained a `ready` gate (or be resuming an approved
    request with an equivalent hand-built one). Uses short-lived sessions of
    its own so long-poll continuations can call it after their request
    session is gone.
    """
    with Session(db.engine) as session:
        installed = session.exec(
            select(InstalledIntegration)
            .where(InstalledIntegration.org_id == ctx.org_id)
            .where(InstalledIntegration.integration_id == integration_id)
        ).first()
        if not installed:
            return ExecutionOutcome(
                outcome="error",
                result={},
                error=f"Integration '{integration_id}' not found.",
                duration_ms=0,
                failure="integration_not_found",
            )
        oauth_state = session.exec(
            select(OAuthState)
            .where(OAuthState.org_id == ctx.org_id)
            .where(OAuthState.integration_id == integration_id)
        ).first()
        # Reload any attributes expired by prior commits, then detach so they
        # remain accessible after the session closes.
        session.refresh(installed)
        session.expunge(installed)
        if oauth_state:
            session.refresh(oauth_state)
            session.expunge(oauth_state)

    # Pre-flight: refresh if the token is known to be expired.
    if (
        installed.auth_method == "oauth"
        and oauth_state
        and oauth_refresh.is_token_expired(oauth_state)
    ):
        refreshed = await oauth_refresh.refresh_tokens(oauth_state)
        if refreshed:
            oauth_state = refreshed

    bundled = integration_registry.get(installed.integration_id, org_id=installed.org_id)
    is_api = isinstance(bundled, CustomIntegration)

    start = time.time()
    if is_api:
        tool_def = api_client.get_tool_def(bundled, tool_name)
        if not tool_def:
            return ExecutionOutcome(
                outcome="error",
                result={},
                error=f"Tool '{tool_name}' not found in integration '{integration_id}'.",
                duration_ms=0,
                failure="tool_not_found",
            )

        async def call(state):
            return await api_client.call_tool(
                installed, tool_def, arguments, state, integration=bundled
            )
    else:

        async def call(state):
            return await mcp_client.call_tool(installed, tool_name, arguments, state)

    result, last_error = await _attempt_with_refresh(call, installed, oauth_state)
    duration_ms = int((time.time() - start) * 1000)

    outcome = "executed" if last_error is None else "error"
    error_str = str(last_error) if last_error else None
    result_json = json.dumps(result) if result and last_error is None else None

    with Session(db.engine) as session:
        log = session.get(LogEntry, gate.pending_log_id) if gate.pending_log_id else None
        if log:
            # Resolve the gate log in place so the whole gate → approved →
            # executed lifecycle reads as one entry.
            log.outcome = outcome
            log.result_json = result_json
            log.error = error_str
            log.duration_ms = duration_ms
            log.access_reason = gate.access_reason
            if ctx.additional_info and not log.additional_info:
                log.additional_info = ctx.additional_info
            if ctx.impersonator_user_id is not None and log.impersonator_user_id is None:
                log.impersonator_user_id = ctx.impersonator_user_id
            session.add(log)
        else:
            session.add(
                LogEntry(
                    org_id=ctx.org_id,
                    integration_id=integration_id,
                    tool_name=tool_name,
                    args_json=json.dumps(arguments),
                    args_hash=gate.args_hash,
                    approval_request_id=gate.approval_request_id,
                    access_reason=gate.access_reason,
                    result_json=result_json,
                    error=error_str,
                    duration_ms=duration_ms,
                    outcome=outcome,
                    requester_ip=ctx.requester_ip,
                    user_agent=ctx.user_agent,
                    api_key_label=ctx.api_key_label,
                    api_key_prefix=ctx.api_key_prefix,
                    impersonator_user_id=ctx.impersonator_user_id,
                    additional_info=ctx.additional_info,
                )
            )
        session.commit()

    if last_error is not None:
        logger.warning("Tool call failed for %s/%s: %s", integration_id, tool_name, last_error)
    else:
        event_name = "tool_called_impersonated" if ctx.impersonator_user_id else "tool_called"
        posthog_client.capture(
            distinct_id=str(ctx.org_id),
            event=event_name,
            properties={
                "integration_id": integration_id,
                "tool_name": tool_name,
                "duration_ms": duration_ms,
                "access_reason": gate.access_reason,
                "source": ctx.source,
                "impersonator_user_id": (
                    str(ctx.impersonator_user_id) if ctx.impersonator_user_id else None
                ),
            },
        )

    return ExecutionOutcome(
        outcome=outcome, result=result, error=error_str, duration_ms=duration_ms
    )
