import logging
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from sqlmodel import Session, select

from sutr.config import settings
from sutr.models.secret import Secret
from sutr.secrets import secrets_backend

logger = logging.getLogger(__name__)

TSecretOwner = TypeVar("TSecretOwner")

# The backend that wrote plaintext. Rows labelled with it are what the
# re-encryption sweep looks for.
LEGACY_PLAINTEXT_BACKEND = "db"


def upsert_secret(
    session: Session,
    *,
    org_id: UUID | None,
    kind: str,
    ref: str,
    value: str,
    secret_id: UUID | None = None,
) -> Secret:
    stored = secrets_backend.store(ref, value)
    secret = session.get(Secret, secret_id) if secret_id else None
    now = datetime.utcnow()
    if not secret:
        secret = Secret(
            org_id=org_id,
            kind=kind,
            storage_backend=settings.secrets_backend,
            created_at=now,
        )
    secret.org_id = org_id
    secret.kind = kind
    secret.storage_backend = settings.secrets_backend
    # The reference, so an external store can be read back from. Recorded for
    # every backend, not only the ones that need it: a row that says where its
    # secret is remains readable after a backend change.
    secret.ref = ref
    secret.value = stored.value
    secret.encrypted_data_key = stored.encrypted_data_key
    secret.kms_key_id = stored.kms_key_id
    secret.updated_at = now
    session.add(secret)
    session.flush()
    return secret


def get_secret_value(session: Session, secret_id: UUID | None) -> str | None:
    if not secret_id:
        return None
    secret = session.get(Secret, secret_id)
    if not secret:
        return None
    return secrets_backend.retrieve(secret)


def delete_secret(session: Session, secret_id: UUID | None) -> None:
    if not secret_id:
        return
    secret = session.get(Secret, secret_id)
    if not secret:
        return
    secrets_backend.delete(secret)
    session.delete(secret)
    session.flush()


def reencrypt_legacy_secrets(session: Session, *, batch_size: int = 200) -> int:
    """Re-store every row the plaintext `db` backend wrote through the active backend.

    One-time upgrade work, written so it can run on every start: it selects
    only rows still labelled `db`, so a second pass finds nothing and costs
    one query. Each converted row is committed in batches; a crash midway
    leaves the rest for the next pass rather than rolling everything back.

    Returns the number of rows converted. A no-op when the active backend is
    itself `db` (an operator who chose plaintext explicitly keeps it) and
    when there is nothing to convert.
    """
    active = settings.secrets_backend
    if active == LEGACY_PLAINTEXT_BACKEND:
        return 0

    converted = 0
    while True:
        rows = session.exec(
            select(Secret)
            .where(Secret.storage_backend == LEGACY_PLAINTEXT_BACKEND)
            .where(Secret.value.is_not(None))  # type: ignore[union-attr]
            .limit(batch_size)
        ).all()
        if not rows:
            break
        for secret in rows:
            stored = secrets_backend.store(secret.ref, secret.value or "")
            secret.value = stored.value
            secret.encrypted_data_key = stored.encrypted_data_key
            secret.kms_key_id = stored.kms_key_id
            secret.storage_backend = active
            secret.updated_at = datetime.utcnow()
            session.add(secret)
            converted += 1
        session.commit()
        if len(rows) < batch_size:
            break

    if converted:
        logger.info("secrets: re-encrypted %d legacy plaintext row(s) via %s", converted, active)
    return converted
