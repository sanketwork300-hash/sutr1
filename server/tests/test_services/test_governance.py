"""Phase 5 governance: audit trail, decision locking, approve-exact-forever,
tools:execute enforcement, log redaction, retention, arg canonicalization."""

import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from sutr.approvals.normalize import hash_normalized_args, normalize_tool_args
from sutr.approvals.requests import (
    get_or_create_approval_request,
    try_consume_approved_request,
)
from sutr.maintenance import run_maintenance_sweep
from sutr.models.audit_event import AuditEvent
from sutr.models.integration import InstalledIntegration
from sutr.models.log import LogEntry
from sutr.models.org_membership import OrgMembership
from sutr.models.tool_approval_request import ToolApprovalRequest
from sutr.models.tool_execution import ToolExecutionSetting

MOCK_RESULT = {"content": [{"type": "text", "text": "done"}]}


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


def _set_role(session, test_user, test_org, role: str) -> None:
    membership = session.get(OrgMembership, (test_user.id, test_org.id))
    membership.role = role
    session.add(membership)
    session.commit()


def _audit_actions(session, org_id) -> list[str]:
    return [
        e.action for e in session.exec(select(AuditEvent).where(AuditEvent.org_id == org_id)).all()
    ]


# ── Audit trail ──────────────────────────────────────────────────────────────


async def test_policy_change_writes_audit_with_old_and_new(client, session, test_org):
    resp = await client.put("/api/tool-settings/posthog/create_annotation", json={"mode": "deny"})
    assert resp.status_code == 200

    session.expire_all()
    event = session.exec(select(AuditEvent).where(AuditEvent.action == "policy.mode_changed")).one()
    meta = json.loads(event.metadata_json)
    assert meta["old_mode"] == "require_approval"
    assert meta["new_mode"] == "deny"
    assert event.target_id == "posthog/create_annotation"
    assert event.actor_user_id is not None


async def test_api_key_lifecycle_is_audited(client, session, test_org):
    resp = await client.post("/api/api-keys", json={"name": "audit-key"})
    assert resp.status_code == 201
    key_id = resp.json()["id"]

    resp = await client.delete(f"/api/api-keys/{key_id}")
    assert resp.status_code == 204

    session.expire_all()
    actions = _audit_actions(session, test_org.id)
    assert "api_key.created" in actions
    assert "api_key.revoked" in actions


async def test_approval_decision_is_audited(client, session, test_org, installed_posthog):
    req = get_or_create_approval_request(
        session, test_org.id, "posthog", "create_annotation", {"content": "hi"}
    )
    resp = await client.post(f"/api/tool-approvals/requests/{req.id}/deny", json={})
    assert resp.status_code == 200

    session.expire_all()
    event = session.exec(select(AuditEvent).where(AuditEvent.action == "approval.denied")).one()
    assert event.target_id == str(req.id)


async def test_audit_endpoint_lists_and_enforces_permission(client, session, test_user, test_org):
    await client.put("/api/tool-settings/posthog/x", json={"mode": "deny"})

    resp = await client.get("/api/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert any(e["action"] == "policy.mode_changed" for e in body)

    _set_role(session, test_user, test_org, "viewer")
    resp = await client.get("/api/audit")
    assert resp.status_code == 403


# ── Decision-row locking ─────────────────────────────────────────────────────


def test_approve_once_grant_consumed_exactly_once(session, test_org, installed_posthog):
    req = get_or_create_approval_request(
        session, test_org.id, "posthog", "create_annotation", {"content": "hi"}
    )
    req.status = "approved"
    req.decision_mode = "approve_once"
    session.add(req)
    session.commit()

    first = try_consume_approved_request(
        session, test_org.id, "posthog", "create_annotation", req.args_hash
    )
    second = try_consume_approved_request(
        session, test_org.id, "posthog", "create_annotation", req.args_hash
    )
    assert first is not None and first.status == "consumed"
    assert second is None


async def test_second_decision_conflicts(client, session, test_org, installed_posthog):
    req = get_or_create_approval_request(
        session, test_org.id, "posthog", "create_annotation", {"content": "hi"}
    )
    resp = await client.post(f"/api/tool-approvals/requests/{req.id}/approve-once", json={})
    assert resp.status_code == 200
    resp = await client.post(f"/api/tool-approvals/requests/{req.id}/deny", json={})
    assert resp.status_code == 409


# ── Approve exact args forever ───────────────────────────────────────────────


async def test_approve_exact_forever_flow(client, session, test_org, installed_posthog):
    call = {"tool_name": "create_annotation", "args": {"content": "hi"}}

    # 1. Gated.
    resp = await client.post("/api/tools/posthog/call", json=call)
    assert resp.status_code == 403
    request_id = resp.json()["approval_request_id"]

    # 2. Human approves these exact args forever.
    resp = await client.post(f"/api/tool-approvals/requests/{request_id}/approve-exact", json={})
    assert resp.status_code == 200
    assert resp.json()["decision_mode"] == "approve_exact_forever"

    # 3. Same args execute — repeatedly, the grant is never consumed.
    with patch("sutr.mcp.client.call_tool", new_callable=AsyncMock, return_value=MOCK_RESULT):
        for _ in range(2):
            resp = await client.post("/api/tools/posthog/call", json=call)
            assert resp.status_code == 200

    session.expire_all()
    req = session.get(ToolApprovalRequest, uuid.UUID(request_id))
    assert req.status == "approved"  # never transitioned to consumed

    executed = session.exec(
        select(LogEntry).where(LogEntry.org_id == test_org.id).where(LogEntry.outcome == "executed")
    ).all()
    assert len(executed) == 2
    assert all(log.access_reason == "approved_exact" for log in executed)

    # 4. Different args go back through the gate.
    resp = await client.post(
        "/api/tools/posthog/call",
        json={"tool_name": "create_annotation", "args": {"content": "DIFFERENT"}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "approval_required"


# ── tools:execute enforcement ────────────────────────────────────────────────


async def test_viewer_cannot_execute_tools(client, session, test_user, test_org, installed_posthog):
    session.add(
        ToolExecutionSetting(
            org_id=test_org.id,
            integration_id="posthog",
            tool_name="create_annotation",
            mode="allow",
        )
    )
    session.commit()
    _set_role(session, test_user, test_org, "viewer")

    resp = await client.post(
        "/api/tools/posthog/call",
        json={"tool_name": "create_annotation", "args": {"content": "hi"}},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "permission_denied"


# ── Log redaction ────────────────────────────────────────────────────────────


async def test_logs_redact_credentials_but_approval_args_do_not(
    client, session, test_org, installed_posthog
):
    args = {"content": "hi", "api_key": "sk_live_supersecret", "nested": {"password": "hunter2"}}
    resp = await client.post(
        "/api/tools/posthog/call", json={"tool_name": "create_annotation", "args": args}
    )
    assert resp.status_code == 403  # gated

    session.expire_all()
    log = session.exec(select(LogEntry).where(LogEntry.org_id == test_org.id)).one()
    stored = json.loads(log.args_json)
    assert stored["api_key"] == "[REDACTED]"
    assert stored["nested"]["password"] == "[REDACTED]"
    assert stored["content"] == "hi"

    # The approval request keeps the real args — they are re-executed verbatim.
    req = session.exec(
        select(ToolApprovalRequest).where(ToolApprovalRequest.org_id == test_org.id)
    ).one()
    assert json.loads(req.args_json)["api_key"] == "sk_live_supersecret"


# ── Retention ────────────────────────────────────────────────────────────────


def test_retention_prunes_old_logs_but_never_audit(session, test_user, test_org):
    test_org.log_retention_days = 7
    session.add(test_org)
    old = LogEntry(
        org_id=test_org.id,
        timestamp=datetime.utcnow() - timedelta(days=30),
        integration_id="posthog",
        tool_name="x",
        outcome="executed",
    )
    fresh = LogEntry(
        org_id=test_org.id,
        timestamp=datetime.utcnow() - timedelta(days=1),
        integration_id="posthog",
        tool_name="x",
        outcome="executed",
    )
    ancient_audit = AuditEvent(
        org_id=test_org.id,
        timestamp=datetime.utcnow() - timedelta(days=400),
        action="auth.login",
        summary="old audit row",
    )
    session.add(old)
    session.add(fresh)
    session.add(ancient_audit)
    session.commit()

    counts = run_maintenance_sweep()
    assert counts["logs_pruned"] == 1

    session.expire_all()
    remaining = session.exec(select(LogEntry).where(LogEntry.org_id == test_org.id)).all()
    assert len(remaining) == 1
    audits = session.exec(select(AuditEvent).where(AuditEvent.org_id == test_org.id)).all()
    assert len(audits) == 1  # audit rows are exempt from retention


# ── Argument canonicalization ────────────────────────────────────────────────


def test_normalization_canonicalizes_unicode_and_numbers():
    composed = {"name": "caf\u00e9"}  # e-acute as one code point
    decomposed = {"name": "cafe\u0301"}  # e + combining acute accent
    assert hash_normalized_args(normalize_tool_args(composed)) == hash_normalized_args(
        normalize_tool_args(decomposed)
    )

    assert hash_normalized_args(normalize_tool_args({"n": 1.0})) == hash_normalized_args(
        normalize_tool_args({"n": 1})
    )
    # Booleans must not be coerced into numbers.
    assert hash_normalized_args(normalize_tool_args({"b": True})) != hash_normalized_args(
        normalize_tool_args({"b": 1})
    )
    # Non-integral floats keep their value.
    assert normalize_tool_args({"n": 1.5}) == '{"n":1.5}'
