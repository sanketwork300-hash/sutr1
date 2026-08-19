"""Typed returns for the core flow.

Only the values an agent actually branches on get a dataclass. Long-tail
endpoints (usage, logs, catalog, deployments) return the parsed JSON as-is so
the SDK never becomes the reason a new server field is unreachable — each
dataclass also keeps its source dict in `raw` for the same reason.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Tool:
    """A tool exposed by an installed integration."""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    integration_id: str | None = None
    # "allow" | "require_approval" | "deny" — the policy at listing time.
    execution_mode: str | None = None
    category: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def required_params(self) -> list[str]:
        return list(self.input_schema.get("required") or [])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tool":
        return cls(
            name=data.get("name", ""),
            description=data.get("description"),
            input_schema=data.get("inputSchema") or {},
            integration_id=data.get("integration_id"),
            execution_mode=data.get("execution_mode"),
            category=data.get("category"),
            raw=data,
        )


@dataclass
class ToolResult:
    """The result of a tool execution (MCP-shaped content blocks)."""

    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False
    status_code: int | None = None
    duration_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """All text blocks joined — what you usually want to hand to a model."""
        return "\n".join(
            block["text"] for block in self.content if isinstance(block.get("text"), str)
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolResult":
        content = data.get("content")
        return cls(
            content=content if isinstance(content, list) else [],
            is_error=bool(data.get("isError", False)),
            status_code=data.get("status_code"),
            duration_ms=data.get("duration_ms"),
            raw=data,
        )


@dataclass
class ApprovalDecision:
    """The state of an approval request after waiting on it."""

    approval_request_id: str
    integration_id: str
    tool_name: str
    # "pending" | "approved" | "denied" | "expired" | "consumed" | "auto_approved"
    status: str
    message: str
    expires_at: str | None = None
    decision_mode: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    @property
    def pending(self) -> bool:
        return self.status == "pending"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalDecision":
        return cls(
            approval_request_id=str(data.get("approval_request_id", "")),
            integration_id=data.get("integration_id", ""),
            tool_name=data.get("tool_name", ""),
            status=data.get("status", ""),
            message=data.get("message", ""),
            expires_at=data.get("expires_at"),
            decision_mode=data.get("decision_mode"),
            raw=data,
        )


@dataclass
class Integration:
    """An entry in the integration catalog."""

    id: str
    name: str
    type: str | None = None
    description: str | None = None
    auth_methods: list[str] = field(default_factory=list)
    installed: bool = False
    connected: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, installed: dict[str, Any] | None = None
    ) -> "Integration":
        auth = data.get("auth") or []
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            type=data.get("type"),
            description=data.get("description"),
            auth_methods=[a.get("method") for a in auth if isinstance(a, dict)],
            installed=installed is not None,
            connected=bool((installed or {}).get("connected", False)),
            raw=data,
        )
