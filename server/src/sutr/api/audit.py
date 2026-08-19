from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, col, select

from sutr.authz import require_permission
from sutr.db import get_session
from sutr.dependencies import get_current_org
from sutr.models.audit_event import AuditEvent
from sutr.models.org import Org

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def list_audit_events(
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    current_org: Org = Depends(get_current_org),
    _=Depends(require_permission("audit:read")),
) -> list[dict]:
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.org_id == current_org.id)
        .order_by(col(AuditEvent.id).desc())
    )
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if target_type:
        stmt = stmt.where(AuditEvent.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    stmt = stmt.offset(offset).limit(limit)
    return [e.model_dump() for e in session.exec(stmt).all()]
