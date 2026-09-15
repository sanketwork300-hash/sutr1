"""Reading credentials — the half the hot path needs.

Split from `store.py` deliberately (ADR-002). Resolving a credential happens on
every tool call, so it belongs to the data plane; creating and revoking one is
a control-plane operation. Keeping the read side here means the executor never
has to import the writer, and the plane boundary holds rather than being
asserted.
"""

import uuid

from sqlmodel import Session, select

from sutr.models.integration_credential import IntegrationCredential


def list_credentials(
    session: Session, org_id: uuid.UUID, integration_id: str
) -> list[IntegrationCredential]:
    return list(
        session.exec(
            select(IntegrationCredential)
            .where(IntegrationCredential.org_id == org_id)
            .where(IntegrationCredential.integration_id == integration_id)
            .order_by(IntegrationCredential.scheme_name)
        ).all()
    )


def get_credential(
    session: Session, org_id: uuid.UUID, integration_id: str, scheme_name: str
) -> IntegrationCredential | None:
    return session.exec(
        select(IntegrationCredential)
        .where(IntegrationCredential.org_id == org_id)
        .where(IntegrationCredential.integration_id == integration_id)
        .where(IntegrationCredential.scheme_name == scheme_name)
    ).first()
