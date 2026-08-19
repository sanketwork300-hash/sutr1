import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


@dataclass
class ProviderStatus:
    """Live state as reported by the provider."""

    state: str  # "running" | "stopped" | "not_found" | "error"
    detail: str | None = None


class DeploymentProvider(ABC):
    """One deployment target (local Docker, Kubernetes, Argo CD, ...).

    Methods that operate on an existing deployment receive the opaque `state`
    dict the provider itself returned from `deploy()`.
    """

    id: str
    display_name: str

    @abstractmethod
    async def available(self) -> tuple[bool, str | None]:
        """(usable, reason-if-not) — checked before create and surfaced in the UI."""

    @abstractmethod
    async def deploy(self, spec: DeploySpec) -> dict:
        """Build + run the package. Returns the provider state dict; must
        include a `url` key pointing at the served MCP endpoint."""

    @abstractmethod
    async def status(self, state: dict) -> ProviderStatus: ...

    @abstractmethod
    async def start(self, state: dict) -> None: ...

    @abstractmethod
    async def stop(self, state: dict) -> None: ...

    @abstractmethod
    async def remove(self, state: dict) -> None:
        """Tear down everything the deployment created. Must be idempotent."""

    @abstractmethod
    async def logs(self, state: dict, tail: int = 100) -> str: ...


class ProviderError(Exception):
    """A provider operation failed; the message is safe to surface to the user."""
