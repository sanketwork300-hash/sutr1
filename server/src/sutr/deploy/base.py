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
    # Runtime secrets injected as environment variables — never baked into
    # the image or the package files.
    env: dict[str, str] = field(default_factory=dict)
    internal_port: int = 8000
    target: ProviderTarget = field(default_factory=ProviderTarget)


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


class ProviderError(Exception):
    """A provider operation failed; the message is safe to surface to the user."""
