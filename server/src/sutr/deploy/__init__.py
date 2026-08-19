"""Sutr deployment engine: run generated MCP server packages on real targets.

Providers implement one narrow interface (`DeploymentProvider`) so Kubernetes,
Argo CD, and Swaraj Cloud can slot in behind the same API as the local Docker
provider. Provider-specific handles (container ids, pod names, app names)
live in an opaque per-deployment state dict — the models and API never learn
provider internals.
"""
