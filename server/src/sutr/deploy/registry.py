"""Deployment provider registry.

The local Docker provider is disabled on cloud (multi-tenant) instances:
running tenant-supplied containers on the API host is not a safe multi-tenant
operation. Self-hosted installs can additionally turn it off with
DEPLOY_DOCKER_ENABLED=false. Kubernetes / Argo CD / Swaraj Cloud providers
register here when implemented.
"""

from sutr.config import settings
from sutr.deploy.base import DeploymentProvider
from sutr.deploy.docker_provider import DockerProvider

_PROVIDERS: dict[str, DeploymentProvider] = {
    DockerProvider.id: DockerProvider(),
}


def provider_enabled(provider_id: str) -> tuple[bool, str | None]:
    if provider_id not in _PROVIDERS:
        return False, f"Unknown provider '{provider_id}'."
    if provider_id == "docker":
        if settings.is_cloud:
            return False, "The local Docker provider is not available on cloud instances."
        if not settings.deploy_docker_enabled:
            return False, "The Docker provider is disabled (DEPLOY_DOCKER_ENABLED=false)."
    return True, None


def get_provider(provider_id: str) -> DeploymentProvider | None:
    enabled, _ = provider_enabled(provider_id)
    return _PROVIDERS.get(provider_id) if enabled else None


def list_providers() -> list[dict]:
    entries = []
    for provider in _PROVIDERS.values():
        enabled, reason = provider_enabled(provider.id)
        entries.append(
            {
                "id": provider.id,
                "display_name": provider.display_name,
                "enabled": enabled,
                "reason": reason,
            }
        )
    return entries
