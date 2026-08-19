from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from sutr.authz import OrgContext, require_permission
from sutr.config import settings
from sutr.db import get_session
from sutr.dependencies import get_current_org
from sutr.models.org import Org
from sutr.services.audit import record_audit

router = APIRouter(prefix="/api/org-settings", tags=["org-settings"])

# Bounds for the per-org approval expiry window. The lower bound keeps the
# UI usable (giving an approver time to react), the upper bound caps the
# blast radius of a stale pending request.
MIN_APPROVAL_EXPIRY_MINUTES = 1
MAX_APPROVAL_EXPIRY_MINUTES = 1440

# Bounds for tool-call log retention. None (the default) keeps logs forever.
MIN_LOG_RETENTION_DAYS = 1
MAX_LOG_RETENTION_DAYS = 3650


class OrgSettingsResponse(BaseModel):
    approval_expiry_minutes: int
    approval_expiry_minutes_default: int
    approval_expiry_minutes_override: int | None
    log_retention_days: int | None


class UpdateOrgSettingsRequest(BaseModel):
    approval_expiry_minutes: int | None = Field(
        default=None,
        description=(
            "How many minutes a pending approval request stays valid. "
            "Pass null to revert to the instance default."
        ),
    )
    log_retention_days: int | None = Field(
        default=None,
        description=(
            "Delete tool-call logs older than this many days (audit trail is "
            "exempt). Pass null to keep logs forever."
        ),
    )


def _serialize(org: Org) -> OrgSettingsResponse:
    default = settings.approval_expiry_minutes
    return OrgSettingsResponse(
        approval_expiry_minutes=org.approval_expiry_minutes or default,
        approval_expiry_minutes_default=default,
        approval_expiry_minutes_override=org.approval_expiry_minutes,
        log_retention_days=org.log_retention_days,
    )


@router.get("", response_model=OrgSettingsResponse)
def get_settings(
    current_org: Org = Depends(get_current_org),
) -> OrgSettingsResponse:
    return _serialize(current_org)


@router.patch("", response_model=OrgSettingsResponse)
def update_settings(
    body: UpdateOrgSettingsRequest,
    session: Session = Depends(get_session),
    current_org: Org = Depends(get_current_org),
    ctx: OrgContext = Depends(require_permission("org:settings:write")),
) -> OrgSettingsResponse:
    changed: dict[str, dict] = {}

    # Only fields the caller actually sent are applied — null means "revert
    # to default", absent means "leave unchanged".
    if "approval_expiry_minutes" in body.model_fields_set:
        value = body.approval_expiry_minutes
        if value is not None and not (
            MIN_APPROVAL_EXPIRY_MINUTES <= value <= MAX_APPROVAL_EXPIRY_MINUTES
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"approval_expiry_minutes must be between {MIN_APPROVAL_EXPIRY_MINUTES} "
                    f"and {MAX_APPROVAL_EXPIRY_MINUTES}"
                ),
            )
        changed["approval_expiry_minutes"] = {
            "old": current_org.approval_expiry_minutes,
            "new": value,
        }
        current_org.approval_expiry_minutes = value

    if "log_retention_days" in body.model_fields_set:
        value = body.log_retention_days
        if value is not None and not (MIN_LOG_RETENTION_DAYS <= value <= MAX_LOG_RETENTION_DAYS):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"log_retention_days must be between {MIN_LOG_RETENTION_DAYS} "
                    f"and {MAX_LOG_RETENTION_DAYS}"
                ),
            )
        changed["log_retention_days"] = {"old": current_org.log_retention_days, "new": value}
        current_org.log_retention_days = value

    if changed:
        session.add(current_org)
        record_audit(
            session,
            org_id=current_org.id,
            action="org.settings_changed",
            summary="Organization settings changed: " + ", ".join(changed),
            actor_user_id=ctx.user.id,
            target_type="org",
            target_id=str(current_org.id),
            metadata=changed,
        )
        session.commit()
        session.refresh(current_org)
    return _serialize(current_org)
