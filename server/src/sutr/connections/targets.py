"""Where a connected cloud account can actually deploy.

A deployment needs a placement (project, subscription, or account+role) before
it needs anything else, and the user should pick it from what their grant
really covers rather than typing an id from memory. Each provider answers that
question through its own inventory API; the shape returned here is uniform so
the builder renders one picker.
"""

from typing import Any

import httpx

from sutr.connections.device import list_accounts
from sutr.connections.errors import ConnectError

INVENTORY_TIMEOUT_SECONDS = 25


async def _get_json(url: str, token: str, provider_name: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=INVENTORY_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise ConnectError("fetch_failed", f"Could not reach {provider_name}: {exc}")

    if response.status_code in (401, 403):
        raise ConnectError(
            "reauthorization_required",
            f"{provider_name} refused the stored authorization. Reconnect the account.",
        )
    if not response.is_success:
        raise ConnectError(
            "inventory_failed",
            f"{provider_name} returned HTTP {response.status_code} listing deploy targets.",
        )
    try:
        return response.json()
    except ValueError:
        raise ConnectError("inventory_failed", f"{provider_name} returned a non-JSON response.")


async def gcp_projects(token: str) -> list[dict[str, Any]]:
    payload = await _get_json(
        "https://cloudresourcemanager.googleapis.com/v1/projects?pageSize=200",
        token,
        "Google Cloud",
    )
    return [
        {
            "id": project.get("projectId"),
            "label": project.get("name") or project.get("projectId"),
            "detail": project.get("projectNumber", ""),
        }
        for project in payload.get("projects", [])
        # A project pending deletion accepts API calls right up until it does
        # not; offering it as a target only produces a confusing failure.
        if project.get("lifecycleState") == "ACTIVE" and project.get("projectId")
    ]


async def azure_subscriptions(token: str) -> list[dict[str, Any]]:
    payload = await _get_json(
        "https://management.azure.com/subscriptions?api-version=2022-12-01",
        token,
        "Azure",
    )
    return [
        {
            "id": subscription.get("subscriptionId"),
            "label": subscription.get("displayName") or subscription.get("subscriptionId"),
            "detail": subscription.get("state", ""),
        }
        for subscription in payload.get("value", [])
        if subscription.get("subscriptionId") and subscription.get("state") == "Enabled"
    ]


async def aws_accounts(token: str, region: str) -> list[dict[str, Any]]:
    accounts = await list_accounts(region=region, token=token)
    return [
        {
            "id": account["account_id"],
            "label": f"{account['account_name']} ({account['account_id']})",
            "detail": account["email"],
            # Roles are the second half of an AWS placement, so they travel
            # with the account rather than needing a second round trip.
            "roles": account["roles"],
        }
        for account in accounts
    ]
