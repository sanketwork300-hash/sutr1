"""MCP over stdio (build prompt §8, Feature 8).

Some MCP clients only launch subprocesses; they cannot open an HTTP session at
all. This entry point runs the same gateway over stdin/stdout for exactly one
identity, so those clients get the full tool surface — policy, approvals,
logging and metering included, because it is the same `mcp_server` object the
HTTP transports serve.

Identity comes from `SUTR_API_KEY` in the environment. A stdio process has no
request headers to carry one, and an argument would end up in the process
table where anything on the machine can read it.

    SUTR_API_KEY=ap_... uv run sutr-mcp-stdio

This is a single-tenant transport by construction: one process, one key, one
org. It is not a way to serve several tenants.
"""

import asyncio
import hashlib
import logging
import os
import sys

from mcp.server.stdio import stdio_server
from sqlmodel import Session, select

from sutr.db import engine
from sutr.dependencies import AgentAuth
from sutr.mcp.server import RequestMeta, _current_auth, _current_request_meta, mcp_server
from sutr.models.api_key import ApiKey
from sutr.models.org import Org

logger = logging.getLogger(__name__)

ENV_VAR = "SUTR_API_KEY"


def authenticate_from_env() -> AgentAuth | None:
    """Resolve the API key in the environment to an org."""
    raw_key = os.environ.get(ENV_VAR, "").strip()
    if not raw_key:
        return None
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    with Session(engine) as session:
        api_key = session.exec(
            select(ApiKey).where(ApiKey.key_hash == key_hash).where(ApiKey.is_active == True)  # noqa: E712
        ).first()
        if api_key is None:
            return None
        org = session.get(Org, api_key.org_id)
        if org is None:
            return None
        return AgentAuth(org=org, user=None, api_key=api_key)


async def serve() -> int:
    auth = authenticate_from_env()
    if auth is None:
        # stdout is the protocol channel; diagnostics go to stderr or they
        # would corrupt the JSON-RPC stream.
        print(
            f"{ENV_VAR} is not set or is not a valid Sutr API key. "
            "Create one in the console under API keys.",
            file=sys.stderr,
        )
        return 2

    auth_token = _current_auth.set(auth)
    meta_token = _current_request_meta.set(RequestMeta(ip=None, user_agent="sutr-mcp-stdio"))
    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(
                read_stream, write_stream, mcp_server.create_initialization_options()
            )
    finally:
        _current_auth.reset(auth_token)
        _current_request_meta.reset(meta_token)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    raise SystemExit(asyncio.run(serve()))


if __name__ == "__main__":
    main()
