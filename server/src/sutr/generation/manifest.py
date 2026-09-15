"""The MCP manifest: what a runtime artifact is, stated once and immutably.

LLD §3.6 lists a *"# MCP manifest"* as part of the immutable runtime artifact.
The manifest is what a reader — a deployment target, an auditor, the registry —
consults to learn what an artifact contains without unzipping it: which tools,
which transports, which environment variables it needs, which IR and which
template produced it, and the digest of every file in it.

Two properties matter and both are structural:

- **No timestamps, no identifiers that change per run.** The manifest of a
  rebuild from unchanged inputs is byte-identical to the first one. If it were
  not, the build hash derived from it would change on every rebuild and the
  determinism requirement would be untestable.
- **File digests, not a file list.** "The package contains server.py" is not
  provenance. "The package contains this server.py" is.
"""

import hashlib
import json
from typing import Any

MANIFEST_VERSION = 1

# The transports the generated server can actually serve. Listed here rather
# than inferred by a reader from the template text, because a deployment target
# needs to know whether a network endpoint exists before it tries to route to
# one.
TRANSPORTS = ("stdio", "http", "sse")

ENDPOINTS = {
    "health": "/health",
    "metrics": "/metrics",
    "metrics_json": "/metrics.json",
    "mcp": "/mcp",
    "sse": "/sse",
    "messages": "/messages",
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def environment(files: dict[str, str], bundle: dict) -> list[dict[str, Any]]:
    """The environment variables this artifact reads, and why.

    Derived from the bundle rather than from the template text: the credential
    variable's name is chosen per API, and everything else is fixed by the
    governance module. A deployment target reads this to know what it must
    inject — and, just as usefully, what it must *not* inject anything else as.
    """
    entries: list[dict[str, Any]] = []
    env_var = (bundle.get("auth") or {}).get("env_var")
    if env_var:
        entries.append(
            {
                "name": env_var,
                "required": True,
                "secret": True,
                "purpose": "The credential sent to the upstream API on every authenticated call.",
            }
        )
    entries.append(
        {
            "name": "GOVERNANCE_MODE",
            "required": False,
            "secret": False,
            "purpose": "standalone (default) or platform, which requires a signed access pass.",
        }
    )
    for name in ("ACCESS_PASS_ISSUER", "ACCESS_PASS_AUDIENCE", "ACCESS_PASS_SECRET"):
        entries.append(
            {
                "name": name,
                "required": False,
                "secret": name.endswith("SECRET"),
                "purpose": "Required only when GOVERNANCE_MODE=platform.",
            }
        )
    return entries


def build_manifest(
    *,
    files: dict[str, str],
    bundle: dict,
    runtime: str,
    template_version: str,
    ir_version: int | None,
    ir_hash: str,
    knowledge_hash: str,
    knowledge_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the manifest. Deterministic for a given set of inputs."""
    tools = bundle.get("tools") or []
    return {
        "manifest_version": MANIFEST_VERSION,
        "name": bundle.get("name", ""),
        "slug": bundle.get("slug", ""),
        "generator": bundle.get("generator", ""),
        "runtime": runtime,
        "template_version": template_version,
        "source": {
            "ir_version": ir_version,
            "ir_hash": ir_hash,
            "knowledge_hash": knowledge_hash,
            "knowledge": knowledge_summary or {},
        },
        "api": {
            "title": bundle.get("api_title", ""),
            "version": bundle.get("api_version", ""),
            "base_url": bundle.get("base_url", ""),
        },
        "transports": list(TRANSPORTS),
        "endpoints": dict(ENDPOINTS),
        "environment": environment(files, bundle),
        "tool_count": len(tools),
        "tools": [
            {
                "name": tool.get("name", ""),
                "method": tool.get("method", ""),
                "path": tool.get("path", ""),
                "description": tool.get("description", ""),
            }
            for tool in tools
        ],
        "files": [
            {
                "path": path,
                "bytes": len(files[path].encode("utf-8")),
                "sha256": _digest(files[path]),
            }
            for path in sorted(files)
        ],
    }


def canonical_json(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
