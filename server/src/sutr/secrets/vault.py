"""HashiCorp Vault KV v2 as a secrets backend.

LLD §4.3 puts Vault at the centre of the secrets story: *"Runtime → secret
reference → Vault → temporary credential → Provider API. Rotation, versioning,
audit, per-tenant isolation."*

This implements the storage half against Vault's documented KV v2 API:

    GET    {addr}/v1/{mount}/data/{path}       → {"data": {"data": {...}}}
    POST   {addr}/v1/{mount}/data/{path}       ← {"data": {...}}
    DELETE {addr}/v1/{mount}/metadata/{path}   removes every version

**Not** the temporary-credential half. Vault's dynamic secrets engines mint
short-lived provider credentials, and which engine, which role and which lease
policy a provider needs is a per-provider configuration this repository has
never seen. Guessing at it would produce a runtime that fails at call time with
a confusing error instead of at configuration time with a clear one, so
`describe()` reports dynamic credentials as NOT IMPLEMENTED rather than letting
the presence of a Vault backend imply them.

**NOT TESTED against a live Vault.** Every request here is built against the
documented API and asserted in tests against a stub transport. Nothing has run
against a real server, and the traceability matrix says so.
"""

import logging
from typing import Any

import httpx

from sutr.config import settings
from sutr.models.secret import Secret
from sutr.secrets.base import SecretsBackend, StoredSecret

logger = logging.getLogger(__name__)

# The single field one secret is stored under inside its KV document. A fixed
# key rather than a spread of fields: what this platform stores is one opaque
# credential per reference, and a document with an evolving shape would be a
# schema nobody declared.
FIELD = "value"

NOT_CONFIGURED = (
    "NOT_CONFIGURED: the Vault backend needs VAULT_ADDR and VAULT_TOKEN. Set them, or use "
    "SECRETS_BACKEND=db (the default) or db_kms."
)


class VaultError(RuntimeError):
    """A Vault operation failed; the message is safe to surface to an operator."""


def make_client() -> httpx.Client:
    """The HTTP client for one call.

    A module-level function so tests can replace it with one carrying a stub
    transport, and so every request goes through the same timeout and TLS
    settings.
    """
    return httpx.Client(
        base_url=settings.vault_addr.rstrip("/"),
        headers={"X-Vault-Token": settings.vault_token, "Accept": "application/json"},
        timeout=settings.vault_timeout_seconds,
        verify=settings.vault_verify_tls,
        follow_redirects=False,
    )


def _message(payload: Any, status: int) -> str:
    """Vault returns {"errors": [...]}; take the first one."""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            return str(errors[0])
    return f"HTTP {status}"


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"errors": [response.text[:300]]}


def request(method: str, path: str, *, body: Any = None, tolerate: tuple[int, ...] = ()) -> Any:
    if not settings.vault_addr or not settings.vault_token:
        raise VaultError(NOT_CONFIGURED)
    try:
        with make_client() as client:
            response = client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        raise VaultError(f"Could not reach Vault: {exc}")
    if response.status_code in tolerate:
        return None
    detail = _message(_json(response), response.status_code)
    if response.status_code in (401, 403):
        raise VaultError(
            f"Vault refused the request ({response.status_code}). The token may have expired "
            f"or may lack a policy for this path. Details: {detail}"
        )
    if not response.is_success:
        raise VaultError(f"Vault returned {response.status_code}: {detail}")
    return _json(response) if response.content else {}


class VaultSecretsBackend(SecretsBackend):
    """Store secret payloads in Vault's KV v2 engine.

    The database row keeps only the *reference* — the path — which is precisely
    what LLD §4.3.6 asks for: the credential is not in the database. What the
    database holds is a pointer to where it is.
    """

    def __init__(self, mount: str | None = None) -> None:
        self.mount = (mount or settings.vault_mount).strip("/")
        if not settings.vault_addr or not settings.vault_token:
            raise RuntimeError(NOT_CONFIGURED)

    def _path(self, ref: str) -> str:
        prefix = settings.vault_path_prefix.strip("/")
        cleaned = ref.strip("/")
        return f"{prefix}/{cleaned}" if prefix else cleaned

    def store(self, ref: str, value: str) -> StoredSecret:
        request("POST", f"/v1/{self.mount}/data/{self._path(ref)}", body={"data": {FIELD: value}})
        # Nothing sensitive comes back to the caller: the row records where the
        # secret is, never what it is.
        return StoredSecret(value=None, encrypted_data_key=None, kms_key_id=None)

    def retrieve(self, secret: Secret) -> str | None:
        payload = request("GET", f"/v1/{self.mount}/data/{self._path(secret.ref)}", tolerate=(404,))
        if payload is None:
            # nosemgrep: python-logger-credential-disclosure
            logger.warning("vault: no secret at %s", secret.ref)
            return None
        data = ((payload or {}).get("data") or {}).get("data") or {}
        return data.get(FIELD)

    def delete(self, secret: Secret) -> None:
        # The metadata endpoint, not the data one: deleting the data leaves
        # every prior version readable, which is not a delete.
        request("DELETE", f"/v1/{self.mount}/metadata/{self._path(secret.ref)}", tolerate=(404,))


def describe() -> dict[str, Any]:
    """What this install's secret storage actually is."""
    backend = settings.secrets_backend
    configured = bool(settings.vault_addr and settings.vault_token)
    return {
        "backend": backend,
        "vault": {
            "configured": configured,
            "unavailable_reason": None if configured else NOT_CONFIGURED,
            "addr": settings.vault_addr or None,
            "mount": settings.vault_mount,
            "tested_against_a_live_server": False,
        },
        "at_rest": (
            "Vault (KV v2)"
            if backend == "vault"
            else "AES-256-GCM with a KMS-wrapped data key"
            if backend == "db_kms"
            else "plaintext in the database"
        ),
        "dynamic_credentials": {
            "available": False,
            "unavailable_reason": (
                "NOT IMPLEMENTED: minting short-lived provider credentials needs a Vault "
                "secrets engine, a role and a lease policy per provider, none of which is "
                "specified for this platform. Credentials are stored and retrieved, not "
                "generated."
            ),
        },
        "credentials_in_generated_code": False,
        "credentials_injected_as_env_vars": True,
    }
