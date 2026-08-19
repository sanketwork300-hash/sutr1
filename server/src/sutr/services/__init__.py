"""Sutr shared service layer.

Interface surfaces (REST routes, the MCP gateway, and through them the CLI and
SDKs) translate requests into these services and translate results back out.
Business rules — policy evaluation, approval gating, upstream dispatch,
logging, analytics, catalog reads — live here exactly once.
"""
