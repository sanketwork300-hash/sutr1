"""REST surface for tool discovery and execution.

Thin by design: discovery reads go through `services.tool_catalog`, execution
goes through `services.tool_pipeline` (the same pipeline the MCP gateway
uses). This file only translates HTTP in and HTTP out.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from sutr.authz import ensure_agent_can
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.models.integration import InstalledIntegration
from sutr.models.oauth import OAuthState
from sutr.services import tool_catalog
from sutr.services.tool_pipeline import CallContext, evaluate_gate, execute_tool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools", tags=["tools"])


class CallToolRequest(BaseModel):
    tool_name: str
    args: dict = {}
    # Optional free-text explanation the agent can attach to justify the call.
    # Surfaced on approval requests and in logs. Never sent to the upstream tool.
    additional_info: str | None = None


def _call_context(agent_auth: AgentAuth, request: Request, additional_info: str | None):
    return CallContext.from_agent_auth(
        agent_auth,
        source="api",
        requester_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        additional_info=additional_info,
    )


@router.get("")
async def list_all_tools(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    current_org = agent_auth.org
    installed_list = session.exec(
        select(InstalledIntegration).where(InstalledIntegration.org_id == current_org.id)
    ).all()
    all_tools: list[dict] = []
    for installed in installed_list:
        try:
            oauth_state = session.exec(
                select(OAuthState)
                .where(OAuthState.org_id == current_org.id)
                .where(OAuthState.integration_id == installed.integration_id)
            ).first()
            tools = await tool_catalog.get_tools_cached(installed, oauth_state, session)
            for tool in tools:
                tool["integration_id"] = installed.integration_id
            tool_catalog.annotate_tools(tools, session, current_org.id, installed.integration_id)
            all_tools.extend(tools)
        except Exception:
            continue
    return all_tools


@router.get("/{integration_id}")
async def list_tools(
    integration_id: str,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    current_org = agent_auth.org
    installed = session.exec(
        select(InstalledIntegration)
        .where(InstalledIntegration.org_id == current_org.id)
        .where(InstalledIntegration.integration_id == integration_id)
    ).first()
    if not installed:
        raise HTTPException(
            status_code=404, detail=f"Installed integration '{integration_id}' not found"
        )

    oauth_state = session.exec(
        select(OAuthState)
        .where(OAuthState.org_id == current_org.id)
        .where(OAuthState.integration_id == integration_id)
    ).first()
    try:
        tools = await tool_catalog.get_tools_cached(installed, oauth_state, session)
        return tool_catalog.annotate_tools(tools, session, current_org.id, integration_id)
    except Exception as e:
        logger.warning("Failed to list tools for %s: %s", integration_id, e)
        raise HTTPException(
            status_code=502, detail=f"Failed to list tools for integration '{integration_id}'"
        ) from e


@router.post("/{integration_id}/call")
async def call_tool(
    integration_id: str,
    body: CallToolRequest,
    request: Request,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    current_org = agent_auth.org
    # Viewers may browse tools but never execute them (API keys keep their
    # documented capabilities — only human-user contexts are role-checked).
    ensure_agent_can(session, agent_auth, "tools:execute")
    installed = session.exec(
        select(InstalledIntegration)
        .where(InstalledIntegration.org_id == current_org.id)
        .where(InstalledIntegration.integration_id == integration_id)
    ).first()
    if not installed:
        raise HTTPException(
            status_code=404, detail=f"Installed integration '{integration_id}' not found"
        )

    ctx = _call_context(agent_auth, request, body.additional_info)
    gate = evaluate_gate(session, ctx, integration_id, body.tool_name, body.args)

    if gate.status == "denied":
        return JSONResponse(
            status_code=403,
            content={
                "error": "denied",
                "message": "This tool has been blocked and cannot be executed.",
                "integration_id": integration_id,
                "tool_name": body.tool_name,
            },
        )

    if gate.status == "approval_pending":
        return JSONResponse(
            status_code=403,
            content={
                "error": "approval_required",
                "approval_request_id": str(gate.approval_request_id),
                "approval_url": gate.approval_url,
                "message": (
                    "Tool call requires approval before execution. "
                    "Once you receive this request, share the URL with a human and "
                    "explain that this tool call requires approval, which they can "
                    "give using the link. "
                    f"Then share this link in full: {gate.approval_url}"
                ),
                "integration_id": integration_id,
                "tool_name": body.tool_name,
            },
        )

    outcome = await execute_tool(ctx, integration_id, body.tool_name, body.args, gate)

    if outcome.failure == "tool_not_found":
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{body.tool_name}' not found in integration",
        )
    if outcome.failure == "integration_not_found":
        raise HTTPException(
            status_code=404, detail=f"Installed integration '{integration_id}' not found"
        )
    if outcome.error:
        raise HTTPException(
            status_code=502,
            detail=f"Tool call to '{body.tool_name}' on integration '{integration_id}' failed",
        )
    return outcome.result
