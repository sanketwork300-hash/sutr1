"""Provider profiles: how a provider presents itself in the marketplace.

One per organization. Everything on a profile is the provider's own copy —
except `verified`, which they cannot set. A verification badge a provider awards
itself is a badge that means nothing, so it is a platform-administrator action
and carries a note recording on what basis it was given.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from sutr.models.org import Org
from sutr.models.provider_profile import ProviderProfile

EDITABLE = ("display_name", "summary", "website_url", "support_email", "support_url")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get(session: Session, org_id: uuid.UUID) -> ProviderProfile | None:
    return session.exec(select(ProviderProfile).where(ProviderProfile.org_id == org_id)).first()


def upsert(session: Session, org_id: uuid.UUID, changes: dict[str, Any]) -> ProviderProfile:
    """Create or edit a profile. The caller commits."""
    profile = get(session, org_id)
    if profile is None:
        profile = ProviderProfile(org_id=org_id)
    for field in EDITABLE:
        if field in changes and changes[field] is not None:
            setattr(profile, field, str(changes[field]))
    profile.updated_at = _utcnow()
    session.add(profile)
    session.flush()
    return profile


def set_verified(
    session: Session, org_id: uuid.UUID, *, verified: bool, note: str = ""
) -> ProviderProfile:
    """Grant or withdraw the badge. Not reachable from a provider's own routes."""
    profile = upsert(session, org_id, {})
    profile.verified = verified
    profile.verified_note = note
    profile.verified_at = _utcnow() if verified else None
    session.add(profile)
    session.flush()
    return profile


def summary(session: Session, org_id: uuid.UUID) -> dict[str, Any]:
    """The provider block embedded in a listing.

    Falls back to the organization's name when no profile exists, because a
    listing with no provider name at all is worse than one with the plain name
    — and it says which of the two it is, so "unclaimed" is legible.
    """
    profile = get(session, org_id)
    if profile is None:
        org = session.get(Org, org_id)
        return {
            "org_id": str(org_id),
            "display_name": org.name if org is not None else "",
            "verified": False,
            "profile": False,
            "note": "This provider has not set up a profile.",
        }
    org = session.get(Org, org_id)
    return {
        "org_id": str(org_id),
        "display_name": profile.display_name or (org.name if org is not None else ""),
        "summary": profile.summary,
        "website_url": profile.website_url,
        "support_email": profile.support_email,
        "support_url": profile.support_url,
        "verified": profile.verified,
        "verified_note": profile.verified_note or None,
        "verified_at": profile.verified_at.isoformat() if profile.verified_at else None,
        "profile": True,
    }


def serialize(profile: ProviderProfile) -> dict[str, Any]:
    return {
        "org_id": str(profile.org_id),
        "display_name": profile.display_name,
        "summary": profile.summary,
        "website_url": profile.website_url,
        "support_email": profile.support_email,
        "support_url": profile.support_url,
        "verified": profile.verified,
        "verified_note": profile.verified_note or None,
        "verified_at": profile.verified_at.isoformat() if profile.verified_at else None,
        "verification_is_platform_granted": True,
        "updated_at": profile.updated_at.isoformat(),
    }
