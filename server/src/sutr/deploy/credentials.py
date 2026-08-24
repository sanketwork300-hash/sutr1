"""Turn a deployment row into credentials a provider can use right now.

Every provider operation - deploy, status, start, stop, remove, logs - goes
through here, because cloud credentials are short-lived on purpose and there
is no point caching them on the row. For OAuth providers that means an access
token refreshed if due; for AWS it means exchanging the IAM Identity Center
token for role credentials that expire within the hour.
"""

import json

from sqlmodel import Session

from sutr.config import settings
from sutr.connections.device import get_role_credentials
from sutr.connections.errors import ConnectError
from sutr.connections.store import access_token, metadata_of
from sutr.deploy.base import ProviderError, ProviderTarget
from sutr.deploy.registry import get_provider
from sutr.models.deployment import Deployment
from sutr.models.provider_connection import ProviderConnection


def deployment_config(deployment: Deployment) -> dict:
    try:
        config = json.loads(deployment.config_json)
    except ValueError:
        return {}
    return config if isinstance(config, dict) else {}


def _cache_key(deployment: Deployment, config: dict) -> tuple:
    """Two deployments sharing an account and placement share a credential.

    Listing deployments reconciles each row against its provider, and without
    this a page of five AWS deployments would mint five identical sets of role
    credentials before making a single status call.
    """
    return (
        deployment.provider,
        str(deployment.connection_id),
        config.get("account", ""),
        config.get("role", ""),
        config.get("region", ""),
    )


async def resolve_target(
    session: Session, deployment: Deployment, cache: dict | None = None
) -> ProviderTarget:
    """Credentials + placement for one operation on one deployment.

    `cache` is scoped to a single request; credentials are short-lived and must
    never outlive the call that fetched them.
    """
    config = deployment_config(deployment)
    provider = get_provider(deployment.provider)
    if provider is None:
        raise ProviderError(f"The '{deployment.provider}' provider is not available.")
    if provider.connection_provider is None:
        return ProviderTarget(config=config)

    if deployment.connection_id is None:
        raise ProviderError(
            f"This deployment has no connected {provider.display_name} account. "
            "Reconnect the account and create the deployment again."
        )
    connection = session.get(ProviderConnection, deployment.connection_id)
    if connection is None or connection.org_id != deployment.org_id:
        raise ProviderError(
            "The connected account this deployment used has been disconnected. "
            "Reconnect it to manage the deployment."
        )

    key = _cache_key(deployment, config)
    if cache is not None and key in cache:
        return ProviderTarget(config=config, credentials=cache[key])

    try:
        token = await access_token(session, connection)
        if deployment.provider != "aws":
            credentials = {"access_token": token}
        else:
            region = metadata_of(connection).get("sso_region") or settings.aws_sso_region
            role_credentials = await get_role_credentials(
                region=region,
                token=token,
                account_id=config.get("account", ""),
                role_name=config.get("role", ""),
            )
            credentials = {
                "access_key_id": role_credentials.access_key_id,
                "secret_access_key": role_credentials.secret_access_key,
                "session_token": role_credentials.session_token,
            }
    except ConnectError as exc:
        # The provider layer speaks ProviderError; a connection problem is
        # still a reason this deployment cannot proceed.
        raise ProviderError(exc.message)

    if cache is not None:
        cache[key] = credentials
    return ProviderTarget(config=config, credentials=credentials)
