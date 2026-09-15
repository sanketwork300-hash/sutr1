"""The ledger: append-only, and corrected by writing the opposite.

LLD §5.1: *"No financial data is ever deleted; corrections are compensating
transactions."* and *"Ledger: immutable, append-only (usage charge, credit,
refund, tax, settlement, adjustment — debit → credit → balance)."*

There is no update function and no delete function in this module, and that is
the whole design. A mistake is fixed with `reverse`, which writes an entry of
the opposite direction pointing at the one it corrects — so both the mistake and
the correction stay visible. An edited ledger is a ledger nobody can reconcile,
and a deleted entry is a question nobody can answer.

`balance_micros` is the running balance after each entry, computed on append.
It costs a column and buys the property that matters: any entry can be shown
with the balance it produced, without replaying the account.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, col, desc, func, select

from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.models.ledger_entry import (
    CREDIT,
    DEBIT,
    KIND_ADJUSTMENT,
    KIND_CREDIT,
    KIND_REFUND,
    KIND_TAX,
    KIND_USAGE_CHARGE,
    KINDS,
    LedgerEntry,
)

# Which direction each kind moves by default. A charge is owed to the platform
# (debit); a credit or refund moves the other way. `settlement` and
# `adjustment` can go either way, so they must say.
DEFAULT_DIRECTION = {
    KIND_USAGE_CHARGE: DEBIT,
    KIND_TAX: DEBIT,
    KIND_CREDIT: CREDIT,
    KIND_REFUND: CREDIT,
}

ACCOUNT_RECEIVABLE = "receivable"
ACCOUNT_PAYABLE = "payable"


@dataclass
class Balance:
    org_id: uuid.UUID
    account: str
    currency: str
    entries: int
    balance_micros: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "org_id": str(self.org_id),
            "account": self.account,
            "currency": self.currency,
            "entries": self.entries,
            "balance_micros": self.balance_micros,
        }


def current_balance(
    session: Session, *, org_id: uuid.UUID, account: str = ACCOUNT_RECEIVABLE
) -> int:
    latest = session.exec(
        select(LedgerEntry)
        .where(LedgerEntry.org_id == org_id)
        .where(LedgerEntry.account == account)
        .order_by(desc(col(LedgerEntry.created_at)), desc(col(LedgerEntry.id)))
    ).first()
    return latest.balance_micros if latest is not None else 0


def append(
    session: Session,
    *,
    org_id: uuid.UUID,
    kind: str,
    amount_micros: int,
    currency: str = "USD",
    direction: str | None = None,
    account: str = ACCOUNT_RECEIVABLE,
    description: str = "",
    usage_event_id: int | None = None,
    invoice_id: uuid.UUID | None = None,
    settlement_id: uuid.UUID | None = None,
    reverses_entry_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> LedgerEntry:
    """Add one entry. The caller commits. Nothing here ever updates a row."""
    if kind not in KINDS:
        raise InvalidRequestError(f"Unknown ledger kind '{kind}'. Known: {', '.join(KINDS)}.")
    if amount_micros < 0:
        raise InvalidRequestError(
            "A ledger amount is never negative. Direction carries the sign, so a negative "
            "amount would let the same movement be written two ways."
        )
    resolved = direction or DEFAULT_DIRECTION.get(kind)
    if resolved not in (DEBIT, CREDIT):
        raise InvalidRequestError(
            f"A '{kind}' entry has to state its direction: {DEBIT} or {CREDIT}."
        )

    balance = current_balance(session, org_id=org_id, account=account)
    balance += amount_micros if resolved == DEBIT else -amount_micros
    entry = LedgerEntry(
        org_id=org_id,
        account=account,
        kind=kind,
        direction=resolved,
        currency=currency.upper(),
        amount_micros=amount_micros,
        balance_micros=balance,
        description=description,
        usage_event_id=usage_event_id,
        invoice_id=invoice_id,
        settlement_id=settlement_id,
        reverses_entry_id=reverses_entry_id,
        metadata_json=json.dumps(metadata or {}, sort_keys=True),
    )
    session.add(entry)
    session.flush()
    return entry


def reverse(session: Session, entry: LedgerEntry, *, reason: str) -> LedgerEntry:
    """Correct an entry by writing its opposite.

    The compensating transaction §5.1 asks for. Both entries remain: the
    original says what was believed, the reversal says what was decided, and
    the balance ends where it should. Editing the original would erase the
    first half of that story.
    """
    if not reason.strip():
        raise InvalidRequestError(
            "A reversal needs a reason. An unexplained correction is the one thing an auditor "
            "will always ask about."
        )
    existing = session.exec(
        select(LedgerEntry).where(LedgerEntry.reverses_entry_id == entry.id)
    ).first()
    if existing is not None:
        raise ConflictError("That entry has already been reversed.")
    return append(
        session,
        org_id=entry.org_id,
        kind=KIND_ADJUSTMENT,
        amount_micros=entry.amount_micros,
        currency=entry.currency,
        direction=CREDIT if entry.direction == DEBIT else DEBIT,
        account=entry.account,
        description=reason,
        invoice_id=entry.invoice_id,
        settlement_id=entry.settlement_id,
        reverses_entry_id=entry.id,
        metadata={"reverses_kind": entry.kind},
    )


def balance(session: Session, *, org_id: uuid.UUID, account: str = ACCOUNT_RECEIVABLE) -> Balance:
    rows = session.exec(
        select(func.count(), func.max(LedgerEntry.currency))
        .where(LedgerEntry.org_id == org_id)
        .where(LedgerEntry.account == account)
    ).one()
    count, currency = rows
    return Balance(
        org_id=org_id,
        account=account,
        currency=currency or "USD",
        entries=int(count or 0),
        balance_micros=current_balance(session, org_id=org_id, account=account),
    )


def entries_for(
    session: Session,
    *,
    org_id: uuid.UUID,
    account: str | None = None,
    invoice_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[LedgerEntry]:
    statement = select(LedgerEntry).where(LedgerEntry.org_id == org_id)
    if account:
        statement = statement.where(LedgerEntry.account == account)
    if invoice_id is not None:
        statement = statement.where(LedgerEntry.invoice_id == invoice_id)
    return list(
        session.exec(
            statement.order_by(desc(col(LedgerEntry.created_at)), desc(col(LedgerEntry.id))).limit(
                limit
            )
        ).all()
    )


def get(session: Session, entry_id: uuid.UUID, org_id: uuid.UUID) -> LedgerEntry | None:
    entry = session.get(LedgerEntry, entry_id)
    if entry is None or entry.org_id != org_id:
        return None
    return entry


def serialize(entry: LedgerEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "account": entry.account,
        "kind": entry.kind,
        "direction": entry.direction,
        "currency": entry.currency,
        "amount_micros": entry.amount_micros,
        "balance_micros": entry.balance_micros,
        "description": entry.description or None,
        "invoice_id": str(entry.invoice_id) if entry.invoice_id else None,
        "settlement_id": str(entry.settlement_id) if entry.settlement_id else None,
        "reverses_entry_id": (str(entry.reverses_entry_id) if entry.reverses_entry_id else None),
        "metadata": json.loads(entry.metadata_json or "{}"),
        "created_at": entry.created_at.isoformat(),
    }


def describe() -> dict[str, Any]:
    return {
        "kinds": list(KINDS),
        "directions": [DEBIT, CREDIT],
        "accounts": [ACCOUNT_RECEIVABLE, ACCOUNT_PAYABLE],
        "append_only": True,
        "corrections": (
            "A mistake is corrected by appending its opposite, linked by "
            "`reverses_entry_id`. There is no update path and no delete path."
        ),
    }
