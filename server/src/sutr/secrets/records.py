from datetime import datetime
from typing import TypeVar
from uuid import UUID

from sqlmodel import Session

from sutr.config import settings
from sutr.models.secret import Secret
from sutr.secrets import secrets_backend

TSecretOwner = TypeVar("TSecretOwner")


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
