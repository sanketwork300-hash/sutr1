"""Sutr Python SDK — call governed tools from an agent.

    from sutr_sdk import Sutr

    with Sutr(api_key="ap_...", base_url="https://sutr.example.com") as sutr:
        for tool in sutr.list_tools():
            print(tool.integration_id, tool.name, tool.execution_mode)

        result = sutr.call_tool(
            "posthog", "create_annotation", {"content": "deploy"},
            additional_info="Recording the release for the team",
            wait_for_approval=True,
        )
        print(result.text)

Every call goes through Sutr's approval policy: a gated tool raises
`ApprovalRequired` (share `approval_url` with a human), a blocked tool raises
`ToolDenied`. Credentials for the upstream API never leave the Sutr server.
"""

from sutr_sdk.client import AsyncSutr, Sutr
from sutr_sdk.errors import (
    APIError,
    ApprovalPending,
    ApprovalRequired,
    AuthenticationError,
    NotFoundError,
    PermissionDenied,
    RateLimited,
    ServerError,
    SutrConnectionError,
    SutrError,
    ToolDenied,
)
from sutr_sdk.models import ApprovalDecision, Integration, Tool, ToolResult

__version__ = "0.1.0"

__all__ = [
    "Sutr",
    "AsyncSutr",
    "Tool",
    "ToolResult",
    "ApprovalDecision",
    "Integration",
    "SutrError",
    "SutrConnectionError",
    "APIError",
    "AuthenticationError",
    "PermissionDenied",
    "NotFoundError",
    "RateLimited",
    "ServerError",
    "ToolDenied",
    "ApprovalRequired",
    "ApprovalPending",
    "__version__",
]
