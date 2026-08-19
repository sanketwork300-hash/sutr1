"""Canonical pipeline tests: the guarantees both REST and MCP now inherit.

These lock in the semantics that had drifted between the two pre-unification
copies: gate logs use outcome="pending", every log carries requester metadata
and args_hash, consumed approvals resolve their gate log in place with
access_reason="approved_once", and durations are recorded on both surfaces.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from sutr.models.integration import InstalledIntegration
from sutr.models.log import LogEntry
from sutr.models.tool_approval_request import ToolApprovalRequest
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.services.tool_pipeline import (
    CallContext,
    GateResult,
    evaluate_gate,
    execute_tool,
    find_gate_log,
)


@pytest.fixture(name="installed_posthog")
def installed_posthog_fixture(session, test_org):
    installed = InstalledIntegration(
        org_id=test_org.id,
        integration_id="posthog",
        type="remote_mcp",
        url="https://mcp.posthog.com/mcp",
        auth_method="token",
    )
    session.add(installed)
    session.commit()
    return installed


def _ctx(org_id, **overrides) -> CallContext:
    defaults = dict(
        org_id=org_id,
        source="test",
        requester_ip="10.1.2.3",
        user_agent="pytest-agent/1.0",
        additional_info="because the test says so",
    )
    defaults.update(overrides)
    return CallContext(**defaults)


def _set_mode(session, org_id, tool_name, mode):
    session.add(
        ToolExecutionSetting(
            org_id=org_id,
            integration_id="posthog",
            tool_name=tool_name,
            mode=mode,
        )
    )
    session.commit()


# ── evaluate_gate ────────────────────────────────────────────────────────────


def test_gate_denied_writes_full_log(session, test_org, installed_posthog):
    _set_mode(session, test_org.id, "delete_all", "deny")
    ctx = _ctx(test_org.id)

    gate = evaluate_gate(session, ctx, "posthog", "delete_all", {"x": 1})

    assert gate.status == "denied"
    assert gate.args_hash

    log = session.exec(select(LogEntry).where(LogEntry.org_id == test_org.id)).one()
    assert log.outcome == "denied"
    assert log.args_hash == gate.args_hash
    assert log.requester_ip == "10.1.2.3"
    assert log.user_agent == "pytest-agent/1.0"
    assert log.additional_info == "because the test says so"


def test_gate_pending_creates_request_and_single_pending_log(session, test_org, installed_posthog):
    ctx = _ctx(
        test_org.id, api_key_id=uuid.uuid4(), api_key_label="ci-key", api_key_prefix="ap_abc"
    )

    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    assert gate.status == "approval_pending"
    assert gate.approval_request_id is not None
    assert str(gate.approval_request_id) in gate.approval_url

    req = session.get(ToolApprovalRequest, gate.approval_request_id)
    assert req.requester_ip == "10.1.2.3"
    assert req.user_agent == "pytest-agent/1.0"
    assert req.api_key_label == "ci-key"
    assert req.requested_by_agent == f"api_key:{ctx.api_key_id}"

    log = session.exec(select(LogEntry).where(LogEntry.org_id == test_org.id)).one()
    assert log.outcome == "pending"  # canonical spelling — never approval_required
    assert log.approval_request_id == gate.approval_request_id

    # A retry reuses the request and must not duplicate the gate log.
    gate2 = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})
    assert gate2.approval_request_id == gate.approval_request_id
    logs = session.exec(select(LogEntry).where(LogEntry.org_id == test_org.id)).all()
    assert len(logs) == 1


def test_gate_consumes_approved_once(session, test_org, installed_posthog):
    ctx = _ctx(test_org.id)
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})
    req = session.get(ToolApprovalRequest, gate.approval_request_id)
    req.status = "approved"
    req.decision_mode = "approve_once"
    session.add(req)
    session.commit()

    gate2 = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    assert gate2.status == "ready"
    assert gate2.access_reason == "approved_once"
    assert gate2.approval_request_id == gate.approval_request_id
    # It points at the original gate log so execution resolves it in place.
    gate_log = find_gate_log(session, gate.approval_request_id)
    assert gate_log is not None
    assert gate2.pending_log_id == gate_log.id


def test_gate_allow_mode_creates_auto_approved_request(session, test_org, installed_posthog):
    _set_mode(session, test_org.id, "create_annotation", "allow")
    ctx = _ctx(test_org.id)

    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    assert gate.status == "ready"
    assert gate.access_reason == "approved_any"
    req = session.get(ToolApprovalRequest, gate.approval_request_id)
    assert req.status == "auto_approved"
    assert req.requester_ip == "10.1.2.3"


# ── execute_tool ─────────────────────────────────────────────────────────────


async def test_execute_records_duration_and_result(session, test_org, installed_posthog):
    _set_mode(session, test_org.id, "create_annotation", "allow")
    ctx = _ctx(test_org.id)
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    mock_result = {"content": [{"type": "text", "text": "done"}]}
    with patch("sutr.mcp.client.call_tool", new_callable=AsyncMock, return_value=mock_result):
        outcome = await execute_tool(ctx, "posthog", "create_annotation", {"content": "hi"}, gate)

    assert outcome.outcome == "executed"
    assert outcome.error is None
    assert outcome.result == mock_result

    session.expire_all()
    log = session.exec(
        select(LogEntry).where(LogEntry.org_id == test_org.id).where(LogEntry.outcome == "executed")
    ).one()
    assert log.duration_ms is not None
    assert log.access_reason == "approved_any"
    assert json.loads(log.result_json) == mock_result
    assert log.requester_ip == "10.1.2.3"
    assert log.approval_request_id == gate.approval_request_id


async def test_execute_resolves_gate_log_in_place(session, test_org, installed_posthog):
    ctx = _ctx(test_org.id)
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})
    req = session.get(ToolApprovalRequest, gate.approval_request_id)
    req.status = "approved"
    req.decision_mode = "approve_once"
    session.add(req)
    session.commit()

    ready = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})
    assert ready.status == "ready"

    mock_result = {"content": [{"type": "text", "text": "done"}]}
    with patch("sutr.mcp.client.call_tool", new_callable=AsyncMock, return_value=mock_result):
        outcome = await execute_tool(ctx, "posthog", "create_annotation", {"content": "hi"}, ready)

    assert outcome.outcome == "executed"
    session.expire_all()
    logs = session.exec(select(LogEntry).where(LogEntry.org_id == test_org.id)).all()
    # The gate log was resolved in place — one entry for the whole lifecycle.
    assert len(logs) == 1
    assert logs[0].outcome == "executed"
    assert logs[0].access_reason == "approved_once"
    assert logs[0].duration_ms is not None


async def test_execute_error_logs_error_without_result(session, test_org, installed_posthog):
    _set_mode(session, test_org.id, "create_annotation", "allow")
    ctx = _ctx(test_org.id)
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    with patch(
        "sutr.mcp.client.call_tool",
        new_callable=AsyncMock,
        side_effect=RuntimeError("upstream exploded"),
    ):
        outcome = await execute_tool(ctx, "posthog", "create_annotation", {"content": "hi"}, gate)

    assert outcome.outcome == "error"
    assert "upstream exploded" in outcome.error

    session.expire_all()
    log = session.exec(
        select(LogEntry).where(LogEntry.org_id == test_org.id).where(LogEntry.outcome == "error")
    ).one()
    assert log.error == "upstream exploded"
    assert log.result_json is None
    assert log.duration_ms is not None


async def test_execute_unknown_integration_is_pre_dispatch_failure(session, test_org):
    ctx = _ctx(test_org.id)
    gate = GateResult(status="ready", args_hash="x", access_reason="approved_any")

    outcome = await execute_tool(ctx, "not-installed", "tool", {}, gate)

    assert outcome.failure == "integration_not_found"
    logs = session.exec(select(LogEntry).where(LogEntry.org_id == test_org.id)).all()
    assert logs == []


# ── REST surface regression: the drift bug this phase fixes ─────────────────


async def test_rest_gate_log_uses_canonical_pending_outcome(
    client, session, test_org, installed_posthog
):
    """Pre-unification REST wrote outcome="approval_required", which the UI and
    the expiry decoration in /api/logs don't recognize. The pipeline writes
    "pending" for every surface."""
    resp = await client.post(
        "/api/tools/posthog/call",
        json={"tool_name": "create_annotation", "args": {"content": "hi"}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "approval_required"  # HTTP shape is unchanged

    session.expire_all()
    log = session.exec(select(LogEntry).where(LogEntry.org_id == test_org.id)).one()
    assert log.outcome == "pending"
    assert log.requester_ip is not None  # REST now records requester metadata
    assert log.args_hash is not None

    # And /api/logs decorates it with the approval expiry.
    logs_resp = await client.get("/api/logs")
    assert logs_resp.status_code == 200
    entries = logs_resp.json()
    assert entries[0]["outcome"] == "pending"
    assert "approval_expires_at" in entries[0]
