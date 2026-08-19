"""Tool discovery reads: TTL cache with in-progress-refresh coalescing.

Moved verbatim from `api/tools.py` so REST, and any future surface that needs
TTL-refreshing discovery, share one implementation. The MCP management tools
intentionally use a different read strategy (serve any cached list without a
TTL-triggered inline fetch, refreshing in the background) — that is a latency
tradeoff, not drift, and lives in `mcp/management_tools.py`.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime

from sqlmodel import Session, select

from sutr import api_client
from sutr.integrations import registry as integration_registry
from sutr.integrations.types import CustomIntegration
from sutr.mcp import client as mcp_client
from sutr.mcp import oauth as oauth_refresh
from sutr.models.integration import InstalledIntegration
from sutr.models.oauth import OAuthState
from sutr.models.tool_cache import CACHE_TTL, ToolCache
from sutr.models.tool_execution import ToolExecutionSetting

logger = logging.getLogger(__name__)

_CACHE_POLL_INTERVAL = 0.3
_CACHE_POLL_TIMEOUT = 5.0


async def wait_for_in_progress_refresh(
    installed: InstalledIntegration,
    session: Session,
) -> list[dict] | None:
    bind = session.get_bind()
    if bind is None:
        return None

    with Session(bind) as wait_session:
        latest_installed = wait_session.exec(
            select(InstalledIntegration)
            .where(InstalledIntegration.org_id == installed.org_id)
            .where(InstalledIntegration.integration_id == installed.integration_id)
        ).first()
    if not latest_installed or not latest_installed.updating_tool_cache:
        return None

    logger.info(
        "tool_cache WAIT %s (org=%s) refresh already in progress",
        installed.integration_id,
        installed.org_id,
    )
    deadline = asyncio.get_event_loop().time() + _CACHE_POLL_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(_CACHE_POLL_INTERVAL)
        with Session(bind) as wait_session:
            latest_installed = wait_session.exec(
                select(InstalledIntegration)
                .where(InstalledIntegration.org_id == installed.org_id)
                .where(InstalledIntegration.integration_id == installed.integration_id)
            ).first()
            cache = wait_session.exec(
                select(ToolCache)
                .where(ToolCache.org_id == installed.org_id)
                .where(ToolCache.integration_id == installed.integration_id)
            ).first()

        if cache and (datetime.utcnow() - cache.fetched_at) < CACHE_TTL:
            logger.info(
                "tool_cache WAIT-HIT %s (org=%s) age=%.1fs",
                installed.integration_id,
                installed.org_id,
                (datetime.utcnow() - cache.fetched_at).total_seconds(),
            )
            return json.loads(cache.tools_json)

        if not latest_installed or not latest_installed.updating_tool_cache:
            if cache:
                logger.info(
                    "tool_cache WAIT-DONE %s (org=%s) using cache age=%.1fs",
                    installed.integration_id,
                    installed.org_id,
                    (datetime.utcnow() - cache.fetched_at).total_seconds(),
                )
                return json.loads(cache.tools_json)
            logger.info(
                "tool_cache WAIT-DONE %s (org=%s) no cache available after refresh",
                installed.integration_id,
                installed.org_id,
            )
            return None

    logger.info(
        "tool_cache WAIT-TIMEOUT %s (org=%s) after %.1fs",
        installed.integration_id,
        installed.org_id,
        _CACHE_POLL_TIMEOUT,
    )
    return None


def get_execution_modes(session: Session, org_id: uuid.UUID, integration_id: str) -> dict[str, str]:
    settings = session.exec(
        select(ToolExecutionSetting)
        .where(ToolExecutionSetting.org_id == org_id)
        .where(ToolExecutionSetting.integration_id == integration_id)
    ).all()
    return {setting.tool_name: setting.mode for setting in settings}


async def get_tools_cached(
    installed: InstalledIntegration,
    oauth_state: OAuthState | None,
    session: Session,
) -> list[dict]:
    t_start = time.time()
    integration_id = installed.integration_id
    now = datetime.utcnow()
    cache = session.exec(
        select(ToolCache)
        .where(ToolCache.org_id == installed.org_id)
        .where(ToolCache.integration_id == integration_id)
    ).first()

    if cache and (now - cache.fetched_at) < CACHE_TTL:
        age_s = (now - cache.fetched_at).total_seconds()
        logger.info(
            "tool_cache HIT %s (org=%s) age=%.1fs dur=%dms",
            integration_id,
            installed.org_id,
            age_s,
            int((time.time() - t_start) * 1000),
        )
        return json.loads(cache.tools_json)

    waited_tools = await wait_for_in_progress_refresh(installed, session)
    if waited_tools is not None:
        return waited_tools

    miss_reason = (
        "no_row" if not cache else f"stale({(now - cache.fetched_at).total_seconds():.0f}s)"
    )
    logger.info(
        "tool_cache MISS %s (org=%s) reason=%s - fetching upstream",
        integration_id,
        installed.org_id,
        miss_reason,
    )

    # Pre-flight: refresh if token is known to be expired
    if (
        installed.auth_method == "oauth"
        and oauth_state
        and oauth_refresh.is_token_expired(oauth_state)
    ):
        refreshed = await oauth_refresh.refresh_tokens(oauth_state)
        if refreshed:
            oauth_state = refreshed

    # API integrations define tools statically - no remote call needed.
    bundled = integration_registry.get(integration_id, org_id=installed.org_id)
    is_api = isinstance(bundled, CustomIntegration)

    tools: list[dict] | None = None
    last_error: Exception | None = None

    t_upstream = time.time()
    if is_api:
        tools = api_client.list_tools(bundled)
    else:
        for attempt in range(2):
            try:
                tools = await mcp_client.list_tools(installed, oauth_state)
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
    upstream_ms = int((time.time() - t_upstream) * 1000)

    if last_error:
        logger.warning(
            "tool_cache upstream FAIL %s (org=%s) upstream=%dms err=%s cache_fallback=%s",
            integration_id,
            installed.org_id,
            upstream_ms,
            last_error,
            bool(cache),
        )
        if cache:
            return json.loads(cache.tools_json)
        raise last_error

    assert tools is not None

    tools_json = json.dumps(tools)
    if cache:
        cache.tools_json = tools_json
        cache.fetched_at = now
        session.add(cache)
    else:
        session.add(
            ToolCache(
                org_id=installed.org_id,
                integration_id=integration_id,
                tools_json=tools_json,
                fetched_at=now,
            )
        )
    session.commit()
    logger.info(
        "tool_cache WRITE %s (org=%s) tools=%d upstream=%dms total=%dms",
        integration_id,
        installed.org_id,
        len(tools),
        upstream_ms,
        int((time.time() - t_start) * 1000),
    )
    return tools


def annotate_tools(
    tools: list[dict],
    session: Session,
    org_id: uuid.UUID,
    integration_id: str,
) -> list[dict]:
    execution_modes = get_execution_modes(session, org_id, integration_id)
    categories: dict[str, str] = {}
    if integration_id:
        bundled = integration_registry.get(integration_id, org_id=org_id)
        if bundled:
            categories = bundled.tool_categories
    for tool in tools:
        tool_name = tool.get("name", "")
        tool["execution_mode"] = execution_modes.get(tool_name, "require_approval")
        if categories:
            tool["category"] = categories.get(tool_name)
    return tools
