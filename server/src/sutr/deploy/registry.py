"""Deployment provider registry.

Five targets ship: the local Docker provider, one serverless-container
provider per major cloud, and Kubernetes — which is the one that carries the
LLD's runtime isolation requirements (§4.3.8), because a namespace, a
NetworkPolicy and a Pod Security Standard are things only it has.

Availability is answered in two layers, and the distinction matters for the
message the user reads:

- *enabled* — this build and this instance permit the provider at all. Local
  Docker is refused on cloud (multi-tenant) instances because running
  tenant-supplied containers on the API host is not a safe operation, and each
  cloud provider is refused when its OAuth app is not configured.
- *ready* — the specific target the user chose actually works, which needs
  credentials and is therefore checked per deployment in `provider.available`.

Swaraj Cloud is registered as a sixth provider and is permanently disabled with
a `DOCUMENTATION_REQUIRED` reason: its API is not documented anywhere this code
can reach, and inventing it is explicitly forbidden (ADR-006).
"""

from sutr.config import settings
from sutr.connections.providers import is_configured
from sutr.deploy.aws.provider import AwsProvider
from sutr.deploy.azure_provider import AzureProvider
from sutr.deploy.base import DeploymentProvider
from sutr.deploy.docker_provider import DockerProvider
from sutr.deploy.gcp_provider import GcpProvider
from sutr.deploy.kubernetes_provider import KubernetesProvider
from sutr.deploy.swaraj_provider import SwarajCloudProvider

_PROVIDERS: dict[str, DeploymentProvider] = {
    DockerProvider.id: DockerProvider(),
    GcpProvider.id: GcpProvider(),
    AzureProvider.id: AzureProvider(),
    AwsProvider.id: AwsProvider(),
    # Off unless an operator turns it on and points it at a cluster: it needs
    # credentials and a base image, neither of which has a sane default.
    KubernetesProvider.id: KubernetesProvider(),
    # Registered so it is visible and honestly labelled, not hidden.
    # Every operation refuses with DOCUMENTATION_REQUIRED (ADR-006).
    SwarajCloudProvider.id: SwarajCloudProvider(),
}

_CLOUD_TOGGLES = {
    "gcp": "deploy_gcp_enabled",
    "azure": "deploy_azure_enabled",
    "aws": "deploy_aws_enabled",
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

    if provider_id == "kubernetes":
        if not settings.kubernetes_enabled:
            return False, "The Kubernetes provider is disabled (KUBERNETES_ENABLED=false)."
        from sutr.deploy.kubernetes_provider import NOT_CONFIGURED, cluster_config

        if cluster_config() is None or not settings.kubernetes_runtime_image.strip():
            return False, NOT_CONFIGURED
        return True, None

    if provider_id == "swaraj":
        # Not a toggle and not a missing OAuth app: the provider has no
        # implementation at all, and says exactly that.
        from sutr.deploy.swaraj_provider import BLOCKED_REASON

        return False, BLOCKED_REASON

    toggle = _CLOUD_TOGGLES.get(provider_id)
    if toggle and not getattr(settings, toggle):
        return False, f"The {provider_id} provider is disabled ({toggle.upper()}=false)."

    # A cloud provider without its OAuth app is not a provider the user can
    # do anything with, so it reports the operator's missing setup directly.
    connection_provider = _PROVIDERS[provider_id].connection_provider
    if connection_provider:
        configured, reason = is_configured(connection_provider)
        if not configured:
            return False, reason
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
                "connection_provider": provider.connection_provider,
                "creates": provider.creates,
                "config_fields": [
                    {
                        "key": field.key,
                        "label": field.label,
                        "kind": field.kind,
                        "required": field.required,
                        "placeholder": field.placeholder,
                        "default": field.default,
                        "help": field.help,
                    }
                    for field in provider.config_fields
                ],
            }
        )
    return entries
