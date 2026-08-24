"""Deployment provider registry.

Four targets ship: the local Docker provider and one serverless-container
provider per major cloud. Availability is answered in two layers, and the
distinction matters for the message the user reads:

- *enabled* — this build and this instance permit the provider at all. Local
  Docker is refused on cloud (multi-tenant) instances because running
  tenant-supplied containers on the API host is not a safe operation, and each
  cloud provider is refused when its OAuth app is not configured.
- *ready* — the specific target the user chose actually works, which needs
  credentials and is therefore checked per deployment in `provider.available`.
"""

from sutr.config import settings
from sutr.connections.providers import is_configured
from sutr.deploy.aws.provider import AwsProvider
from sutr.deploy.azure_provider import AzureProvider
from sutr.deploy.base import DeploymentProvider
from sutr.deploy.docker_provider import DockerProvider
from sutr.deploy.gcp_provider import GcpProvider

_PROVIDERS: dict[str, DeploymentProvider] = {
    DockerProvider.id: DockerProvider(),
    GcpProvider.id: GcpProvider(),
    AzureProvider.id: AzureProvider(),
    AwsProvider.id: AwsProvider(),
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
