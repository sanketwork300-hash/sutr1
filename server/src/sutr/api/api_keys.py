import hashlib
import secrets
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from sutr.analytics import posthog_client
from sutr.authz import require_permission
from sutr.db import get_session
from sutr.dependencies import get_current_org, get_current_user
from sutr.models.api_key import ApiKey
from sutr.models.org import Org
from sutr.models.user import User
from sutr.services.audit import record_audit

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


class CreateApiKeyRequest(BaseModel):
    name: str


class CreateApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    created_at: datetime
    plain_key: str  # returned once only, never stored


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    is_active: bool


def _generate_key() -> tuple[str, str, str]:
    """Returns (plain_key, key_prefix, key_hash)."""
    plain_key = "ap_" + secrets.token_urlsafe(24)
    key_prefix = plain_key[:12]
    key_hash = hashlib.sha256(plain_key.encode()).hexdigest()
    return plain_key, key_prefix, key_hash


@router.post("", status_code=201)
def create_api_key(
    body: CreateApiKeyRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
    _=Depends(require_permission("api_keys:manage")),
) -> CreateApiKeyResponse:
    plain_key, key_prefix, key_hash = _generate_key()
    api_key = ApiKey(
        org_id=current_org.id,
        created_by_user_id=current_user.id,
        name=body.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
    )
    session.add(api_key)
    record_audit(
        session,
        org_id=current_org.id,
        action="api_key.created",
        summary=f"API key '{body.name}' ({key_prefix}...) created",
        actor_user_id=current_user.id,
        target_type="api_key",
        target_id=key_prefix,
        request=http_request,
    )
    session.commit()
    session.refresh(api_key)
    posthog_client.capture(
        distinct_id=str(current_user.id),
        event="api_key_created",
        properties={"key_prefix": key_prefix},
    )
    return CreateApiKeyResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        created_at=api_key.created_at,
        plain_key=plain_key,
    )


@router.get("")
def list_api_keys(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
) -> list[ApiKeyResponse]:
    keys = session.exec(
        select(ApiKey).where(ApiKey.org_id == current_org.id).order_by(ApiKey.created_at.desc())  # type: ignore[arg-type]
    ).all()
    return [
        ApiKeyResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            is_active=k.is_active,
        )
        for k in keys
    ]


@router.delete("/{key_id}", status_code=204)
def revoke_api_key(
    key_id: uuid.UUID,
    http_request: Request,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
    _=Depends(require_permission("api_keys:manage")),
) -> None:
    api_key = session.exec(
        select(ApiKey).where(ApiKey.id == key_id).where(ApiKey.org_id == current_org.id)
    ).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    api_key.is_active = False
    session.add(api_key)
    record_audit(
        session,
        org_id=current_org.id,
        action="api_key.revoked",
        summary=f"API key '{api_key.name}' ({api_key.key_prefix}...) revoked",
        actor_user_id=current_user.id,
        target_type="api_key",
        target_id=api_key.key_prefix,
        request=http_request,
    )
    session.commit()
    posthog_client.capture(
        distinct_id=str(current_user.id),
        event="api_key_revoked",
        properties={"key_prefix": api_key.key_prefix},
    )
