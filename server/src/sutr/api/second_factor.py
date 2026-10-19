"""Shared TOTP gate for sensitive mutations.

Several endpoints (approval decisions, tool-setting escalations) treat the
same "user with TOTP enabled must present a fresh code" check as a hard
requirement. Keeping the helper here avoids cross-importing private
functions between API modules.

Every failed check counts against the account lockout that protects password
login (`failed_login_attempts` / `locked_until`), and a locked account is
refused before any code is examined. Without that, a stolen session JWT could
be used to grind through the ~1M code space against the approval, policy and
TOTP-disable gates with nothing slowing it down.

Recovery-code verification burns the code onto the user row, so callers
must ``session.add(user)`` and commit after a successful check.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlmodel import Session

from sutr.models.user import User
from sutr.rate_limit import ACCOUNT_LOCKOUT_SECONDS, ACCOUNT_LOCKOUT_THRESHOLD
from sutr.totp import verify_second_factor

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def lockout_retry_after(user: User, now: datetime | None = None) -> int:
    """Seconds until the account may try again, or 0 when it is not locked."""
    if user.locked_until is None:
        return 0
    now = now or _utcnow()
    locked_until = user.locked_until
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if locked_until <= now:
        return 0
    return int((locked_until - now).total_seconds()) + 1


def account_locked() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "error": "totp_locked",
            "message": "Too many failed codes. Try again later.",
        },
    )


def require_not_locked(user: User) -> None:
    retry_after = lockout_retry_after(user)
    if retry_after:
        exc = account_locked()
        exc.headers = {"Retry-After": str(retry_after)}
        raise exc


def record_second_factor_failure(session: Session, user: User) -> None:
    """Count a wrong code against the account and lock it past the threshold. Commits."""
    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
    if user.failed_login_attempts >= ACCOUNT_LOCKOUT_THRESHOLD:
        user.locked_until = _utcnow() + timedelta(seconds=ACCOUNT_LOCKOUT_SECONDS)
        logger.warning(
            "account_locked user_id=%s reason=second_factor attempts=%s",
            user.id,
            user.failed_login_attempts,
        )
    session.add(user)
    session.commit()


def clear_login_failures(session: Session, user: User) -> None:
    """Reset the lockout counters after a fully successful authentication. Does not commit."""
    if user.failed_login_attempts or user.locked_until is not None:
        user.failed_login_attempts = 0
        user.locked_until = None
        session.add(user)


def verify_second_factor_or_raise(session: Session, user: User, code: str | None) -> None:
    """The complete gate: lockout check, verification, failure accounting.

    On success the burned recovery code / TOTP step and the cleared counters
    are staged on ``user``; the caller commits them with its own changes.
    """
    require_not_locked(user)
    if not code:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "totp_required",
                "message": "A one-time code is required to confirm this action.",
            },
        )
    if not verify_second_factor(session, user, code):
        record_second_factor_failure(session, user)
        raise HTTPException(
            status_code=403,
            detail={
                "error": "totp_invalid",
                "message": "That code didn't match — try again with a fresh one.",
            },
        )
    clear_login_failures(session, user)
    session.add(user)


def require_second_factor(session: Session, user: User, code: str | None) -> None:
    """Gate a sensitive mutation on a fresh code when the user has TOTP enabled."""
    if not user.totp_enabled:
        return
    verify_second_factor_or_raise(session, user, code)
