from sutr.config import settings
from sutr.secrets.base import SecretsBackend, StoredSecret
from sutr.secrets.db import DBSecretsBackend


def _build_backend() -> SecretsBackend:
    backend = settings.secrets_backend
    if backend == "db":
        return DBSecretsBackend()
    if backend == "db_kms":
        from sutr.secrets.kms import DBKMSSecretsBackend

        return DBKMSSecretsBackend(
            key_id=settings.secrets_kms_key_id,
            region=settings.secrets_kms_region or None,
        )
    if backend == "vault":
        from sutr.secrets.vault import VaultSecretsBackend

        return VaultSecretsBackend()
    raise ValueError(f"Unknown SECRETS_BACKEND '{backend}'. Supported: db, db_kms, vault")


secrets_backend: SecretsBackend = _build_backend()


def describe() -> dict:
    """What this install stores secrets in, and what it does not do.

    Reported rather than assumed: whether a credential is encrypted at rest,
    and whether it is minted per call or merely stored, are the two things a
    reader most needs and most often guesses at.
    """
    from sutr.secrets.vault import describe as vault_describe

    return vault_describe()


__all__ = ["SecretsBackend", "StoredSecret", "describe", "secrets_backend"]
