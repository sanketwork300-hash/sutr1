from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from sutr.api.second_factor import require_second_factor
from sutr.authz import require_permission
from sutr.db import get_session
from sutr.dependencies import get_current_org, get_current_user
from sutr.models.org import Org
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.models.user import User
from sutr.services.audit import record_audit

router = APIRouter(prefix="/api/tool-settings", tags=["tool-settings"])

VALID_MODES = {"allow", "require_approval", "deny"}


class UpdateSettingRequest(BaseModel):
    mode: str
    totp_code: str | None = None


@router.get("/{integration_id}")
def list_settings(
    integration_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
) -> list[dict]:
    settings = session.exec(
        select(ToolExecutionSetting)
        .where(ToolExecutionSetting.org_id == current_org.id)
        .where(ToolExecutionSetting.integration_id == integration_id)
    ).all()
    return [s.model_dump() for s in settings]


@router.put("/{integration_id}/{tool_name}")
def update_setting(
    integration_id: str,
    tool_name: str,
    body: UpdateSettingRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
    _=Depends(require_permission("policies:write")),
) -> dict:
    if body.mode not in VALID_MODES:
        raise HTTPException(
            status_code=400, detail=f"Invalid mode '{body.mode}'. Must be one of: {VALID_MODES}"
        )

    setting = session.exec(
        select(ToolExecutionSetting)
        .where(ToolExecutionSetting.org_id == current_org.id)
        .where(ToolExecutionSetting.integration_id == integration_id)
        .where(ToolExecutionSetting.tool_name == tool_name)
    ).first()

    # Escalating any tool into "allow" removes the approval gate and must go
    # through the same second-factor challenge as the approval-flow equivalent
    # (see tool_approvals.allow_tool). A missing row counts as the default
    # "require_approval" mode used by evaluate_policy.
    current_mode = setting.mode if setting else "require_approval"
    if current_mode != "allow" and body.mode == "allow":
        require_second_factor(current_user, body.totp_code)
        session.add(current_user)

    if setting:
        setting.mode = body.mode
        setting.updated_by_user_id = current_user.id
        setting.updated_at = datetime.utcnow()
    else:
        setting = ToolExecutionSetting(
            org_id=current_org.id,
            integration_id=integration_id,
            tool_name=tool_name,
            mode=body.mode,
            updated_by_user_id=current_user.id,
            updated_at=datetime.utcnow(),
        )

    session.add(setting)
    # The prior mode is otherwise destroyed invisibly — the audit row is the
    # durable record of what changed, by whom, from where.
    record_audit(
        session,
        org_id=current_org.id,
        action="policy.mode_changed",
        summary=f"{tool_name} on {integration_id}: {current_mode} -> {body.mode}",
        actor_user_id=current_user.id,
        target_type="tool_policy",
        target_id=f"{integration_id}/{tool_name}",
        metadata={
            "integration_id": integration_id,
            "tool_name": tool_name,
            "old_mode": current_mode,
            "new_mode": body.mode,
        },
        request=http_request,
    )
    session.commit()
    session.refresh(setting)
    return setting.model_dump()
