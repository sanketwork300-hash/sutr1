"""Swaraj Cloud deployment provider — interface only.

**BLOCKED — EXTERNAL API DOCUMENTATION REQUIRED.**

The ESDS build brief names Swaraj Cloud as a deployment target and, in the
same breath, records that no implementation exists and that its name appears
only in comments. No API documentation for it exists in this repository, in
the ESDS LLD, or anywhere this code can reach.

So this module implements the `DeploymentProvider` interface completely and
implements none of its behaviour. Every operation refuses with a
`NOT_CONFIGURED / DOCUMENTATION_REQUIRED` message naming exactly what is
missing. The provider is registered and visible rather than hidden, because a
user asking "can I deploy to Swaraj Cloud?" deserves a clear answer instead of
an absence.

What is needed to implement it (ADR-006 in docs/ARCHITECTURE_DECISIONS.md):

- the authentication model (OAuth? API key? signed requests? which audience?)
- the API base URL and version
- endpoints for create / read / update / delete of a container workload
- the request and response schemas for each
- the region model and how a region is selected
- how container images are supplied (a registry? a source upload?)
- how runtime environment variables and secrets are injected
- how logs and metrics are read

Until those arrive, guessing any of them would produce a provider that fails
at deploy time with a confusing error instead of at configuration time with a
clear one. A test pins this refusal so it cannot quietly become a guess.
"""

from sutr.deploy.base import (
    DeploymentProvider,
    DeploySpec,
    ProviderError,
    ProviderMetrics,
    ProviderStatus,
    ProviderTarget,
)

BLOCKED_CODE = "NOT_CONFIGURED / DOCUMENTATION_REQUIRED"

BLOCKED_REASON = (
    f"{BLOCKED_CODE}: the Swaraj Cloud provider is not implemented. Official Swaraj Cloud "
    "API documentation (authentication model, base URL, workload endpoints, request/response "
    "schemas, region model, image delivery, secret injection) is required before it can be. "
    "No part of its API has been guessed."
)


class SwarajCloudProvider(DeploymentProvider):
    id = "swaraj"
    display_name = "Swaraj Cloud"
    # Unknown until the authentication model is documented; declaring one now
    # would be the first invented detail.
    connection_provider = None
    config_fields = ()
    creates = "Nothing yet — the provider is awaiting official API documentation."
    supports_update = False
    supports_metrics = False

    async def available(self, target: ProviderTarget | None = None) -> tuple[bool, str | None]:
        return False, BLOCKED_REASON

    async def deploy(self, spec: DeploySpec) -> dict:
        raise ProviderError(BLOCKED_REASON)

    async def update(self, spec: DeploySpec) -> dict:
        raise ProviderError(BLOCKED_REASON)

    async def rollback(self, spec: DeploySpec) -> dict:
        raise ProviderError(BLOCKED_REASON)

    async def status(self, state: dict, target: ProviderTarget) -> ProviderStatus:
        return ProviderStatus(state="error", detail=BLOCKED_REASON)

    async def start(self, state: dict, target: ProviderTarget) -> None:
        raise ProviderError(BLOCKED_REASON)

    async def stop(self, state: dict, target: ProviderTarget) -> None:
        raise ProviderError(BLOCKED_REASON)

    async def remove(self, state: dict, target: ProviderTarget) -> None:
        # `remove` must be idempotent, and nothing was ever created — so this
        # is the one operation that can honestly succeed by doing nothing.
        return None

    async def logs(self, state: dict, target: ProviderTarget, tail: int = 100) -> str:
        return BLOCKED_REASON

    async def metrics(self, state: dict, target: ProviderTarget) -> ProviderMetrics:
        return ProviderMetrics(source=self.id, unavailable_reason=BLOCKED_REASON)
