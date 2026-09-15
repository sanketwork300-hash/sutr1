"""Per-scheme credentials for compiled API integrations.

Sutr began with one credential per integration, sent as one header. Real
specifications declare several security schemes, place API keys in query
strings and cookies, and expect OAuth2 grants to be *run* rather than replaced
by a pasted token (build prompt §24, ADR-009). This package holds the
credentials for those schemes and resolves them at dispatch time.

Nothing here ever returns a secret to a client: `store.py` writes secret
references, and `resolve.py` produces the credential values only for the
outbound HTTP request.
"""
