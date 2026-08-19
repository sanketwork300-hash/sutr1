"""Workspace CRUD. Every org has a non-deletable default workspace."""

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from agent_port.authz import OrgContext, get_org_context, require_permission
from agent_port.db import get_session
from agent_port.models.org import Org
from agent_port.models.workspace import Workspace

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "workspace"


def get_or_create_default_workspace(session: Session, org: Org) -> Workspace:
    """Runtime accessor for the org's default workspace. Orgs created before
    migration 0023 got one backfilled; this covers any gap defensively."""
    ws = session.exec(
        select(Workspace).where(Workspace.org_id == org.id).where(Workspace.is_default == True)  # noqa: E712
    ).first()
    if ws is None:
        ws = Workspace(
            org_id=org.id,
            name="Default",
            slug="default",
            is_default=True,
            created_at=datetime.now(timezone.utc),
        )
        session.add(ws)
        session.commit()
        session.refresh(ws)
    return ws


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    slug: str
    is_default: bool


class CreateWorkspaceRequest(BaseModel):
    name: str


class UpdateWorkspaceRequest(BaseModel):
    name: str


def _serialize(ws: Workspace) -> WorkspaceResponse:
    return WorkspaceResponse(id=str(ws.id), name=ws.name, slug=ws.slug, is_default=ws.is_default)


@router.get("", response_model=list[WorkspaceResponse])
def list_workspaces(
    ctx: OrgContext = Depends(get_org_context),
    session: Session = Depends(get_session),
) -> list[WorkspaceResponse]:
    get_or_create_default_workspace(session, ctx.org)
    rows = session.exec(select(Workspace).where(Workspace.org_id == ctx.org.id)).all()
    return [_serialize(ws) for ws in sorted(rows, key=lambda w: (not w.is_default, w.name.lower()))]


@router.post("", status_code=201, response_model=WorkspaceResponse)
def create_workspace(
    body: CreateWorkspaceRequest,
    ctx: OrgContext = Depends(require_permission("org:settings:write")),
    session: Session = Depends(get_session),
) -> WorkspaceResponse:
    name = body.name.strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="Workspace name must be 1-80 characters")
    slug = _slugify(name)
    existing = session.exec(
        select(Workspace).where(Workspace.org_id == ctx.org.id).where(Workspace.slug == slug)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A workspace with a similar name already exists"
        )
    ws = Workspace(org_id=ctx.org.id, name=name, slug=slug)
    session.add(ws)
    session.commit()
    session.refresh(ws)
    return _serialize(ws)


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
def rename_workspace(
    workspace_id: uuid.UUID,
    body: UpdateWorkspaceRequest,
    ctx: OrgContext = Depends(require_permission("org:settings:write")),
    session: Session = Depends(get_session),
) -> WorkspaceResponse:
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Workspace not found")
    name = body.name.strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="Workspace name must be 1-80 characters")
    ws.name = name
    session.add(ws)
    session.commit()
    session.refresh(ws)
    return _serialize(ws)


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(
    workspace_id: uuid.UUID,
    ctx: OrgContext = Depends(require_permission("org:settings:write")),
    session: Session = Depends(get_session),
) -> None:
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if ws.is_default:
        raise HTTPException(status_code=409, detail="The default workspace cannot be deleted")
    session.delete(ws)
    session.commit()
