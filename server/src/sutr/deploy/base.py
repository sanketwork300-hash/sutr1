import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ConfigField:
    """One thing a provider must be told before it can deploy.

    Declared rather than hard-coded into the UI so a new provider ships its
    own form, and so the explanation of *why* a value is needed lives next to
    the code that uses it. `kind` drives the control the builder renders:

    - ``text``     free text (registry name, resource group, ...)
    - ``target``   the placement picked from the connected account's inventory
                   (GCP project, Azure subscription, AWS account)
    - ``role``     an AWS Identity Center role, scoped by the chosen account
    - ``region``   a cloud region string
    """

    key: str
    label: str
    kind: str = "text"
    required: bool = True
    placeholder: str = ""
    default: str = ""
    help: str = ""


@dataclass
class ProviderTarget:
    """Resolved credentials plus placement for one provider operation.

    Cloud providers hold no state between calls: every operation is handed a
    fresh credential set, because the credentials are short-lived by design
    (AWS role credentials expire within the hour, OAuth access tokens within
    a few). `config` is the user's placement choice, snapshotted on the
    deployment row so a later stop/start does not depend on the builder form
    still being open.
    """

    config: dict[str, str] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)


@dataclass
class DeploySpec:
    """Everything a provider needs to run one generated MCP server package."""

    deployment_id: uuid.UUID
    name: str
    slug: str
    package_zip: bytes
    # The tenant this runtime belongs to. Providers that isolate per tenant —
    # a Kubernetes namespace, for one — need it, and deriving it from the
    # deployment id would give every deployment its own isolation boundary
    # instead of one per provider (LLD §4.3.8).
    org_id: uuid.UUID | None = None
    # Runtime secrets injected as environment variables — never baked into
    # the image or the package files.
    env: dict[str, str] = field(default_factory=dict)
    internal_port: int = 8000
    target: ProviderTarget = field(default_factory=ProviderTarget)
    # Monotonic revision number for this deployment. Providers must include it
    # in the artifact they build (image tag, object name) so each revision is a
    # distinct, retained artifact — that is what makes a rollback a re-run of
    # something already built rather than a rebuild from source (ADR-016).
    revision: int = 1
    # The provider state of the deployment being replaced, when updating. Lets
    # a provider preserve what should not change across an update — a bound
    # port, an assigned URL — instead of producing a new one.
    previous_state: dict = field(default_factory=dict)

    @property
    def artifact_tag(self) -> str:
        """A per-revision tag, stable for a given (deployment, revision)."""
        return f"{str(self.deployment_id)[:8]}-r{self.revision}"


@dataclass
class ProviderMetrics:
    """Runtime measurements, as far as the provider will report them.

    Every field is optional: providers differ in what they expose, and a
    missing number must read as "not reported" rather than as zero. `source`
    names where the numbers came from so a reader can judge them, and
    `unavailable_reason` explains an empty result instead of leaving it blank.
    """

    cpu_percent: float | None = None
    memory_bytes: int | None = None
    memory_limit_bytes: int | None = None
    request_count: int | None = None
    error_count: int | None = None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    replicas: int | None = None
    healthy: bool | None = None
    source: str = ""
    unavailable_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "cpu_percent": self.cpu_percent,
            "memory_bytes": self.memory_bytes,
            "memory_limit_bytes": self.memory_limit_bytes,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "latency_ms_p50": self.latency_ms_p50,
            "latency_ms_p95": self.latency_ms_p95,
            "replicas": self.replicas,
            "healthy": self.healthy,
            "source": self.source,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass
class ProviderStatus:
    """Live state as reported by the provider."""

    state: str  # "running" | "stopped" | "not_found" | "error"
    detail: str | None = None


class DeploymentProvider(ABC):
    """One deployment target (local Docker, Cloud Run, Container Apps, ...).

    Methods that operate on an existing deployment receive the opaque `state`
    dict the provider itself returned from `deploy()`, plus the target whose
    credentials are valid *now*.
    """

    id: str
    display_name: str
    # Which connected-account provider authorizes this target, or None for a
    # provider that runs on the sutr host itself.
    connection_provider: str | None = None
    # Fields the builder must collect. Empty for the local provider.
    config_fields: tuple[ConfigField, ...] = ()
    # One sentence describing what gets created, shown before the user commits.
    creates: str = ""

    @abstractmethod
    async def available(self, target: ProviderTarget | None = None) -> tuple[bool, str | None]:
        """(usable, reason-if-not) — checked before create and surfaced in the UI."""

    @abstractmethod
    async def deploy(self, spec: DeploySpec) -> dict:
        """Build + run the package. Returns the provider state dict; must
        include a `url` key pointing at the served MCP endpoint."""

    @abstractmethod
    async def status(self, state: dict, target: ProviderTarget) -> ProviderStatus: ...

    @abstractmethod
    async def start(self, state: dict, target: ProviderTarget) -> None: ...

    @abstractmethod
    async def stop(self, state: dict, target: ProviderTarget) -> None: ...

    @abstractmethod
    async def remove(self, state: dict, target: ProviderTarget) -> None:
        """Tear down everything the deployment created. Must be idempotent."""

    @abstractmethod
    async def logs(self, state: dict, target: ProviderTarget, tail: int = 100) -> str: ...

    # ── Update, rollback, metrics ────────────────────────────────────────────
    #
    # Build prompt §36 forbids implementing an update as delete-then-recreate.
    # The default below is not that: every provider here names its resources
    # from the deployment id, so calling `deploy()` again applies a new
    # revision to the *same* named service — which is exactly what Cloud Run,
    # Container Apps and App Runner do natively. The per-revision artifact tag
    # (`spec.artifact_tag`) is what keeps the previous build around, so a
    # rollback re-runs a retained artifact instead of rebuilding.
    #
    # A provider that genuinely cannot update in place must set
    # `supports_update = False` and say so, rather than silently recreating.

    supports_update: bool = True
    supports_metrics: bool = False

    async def update(self, spec: DeploySpec) -> dict:
        """Apply a new revision to an existing deployment.

        Returns the new provider state, same shape as `deploy()`.
        """
        if not self.supports_update:
            raise ProviderError(
                f"The {self.display_name} provider cannot update a deployment in place."
            )
        return await self.deploy(spec)

    async def rollback(self, spec: DeploySpec) -> dict:
        """Re-apply a previously built revision.

        `spec` carries the retained package and the revision number of the
        version being restored, so this is a re-run rather than a rebuild from
        changed source.
        """
        return await self.update(spec)

    async def metrics(self, state: dict, target: ProviderTarget) -> ProviderMetrics:
        """Runtime measurements, where the provider exposes them."""
        return ProviderMetrics(
            source=self.id,
            unavailable_reason=(
                f"The {self.display_name} provider does not report runtime metrics."
            ),
        )


class ProviderError(Exception):
    """A provider operation failed; the message is safe to surface to the user."""
