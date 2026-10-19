"""S3: the raw invite token must not be handed to the inviter when it is emailed.

An inviter holding the token could accept the invitation themselves and
pre-register an account for the invitee's email with a password they control.
The link is only returned when the server has no way to email it, and an
account created from an out-of-band link starts unverified.
"""

import pytest
from sqlmodel import select

from sutr.config import settings
from sutr.models.user import User


@pytest.fixture(name="sent_emails")
def sent_emails_fixture(monkeypatch):
    sent: list[dict] = []

    def _capture(*, to: str, subject: str, html: str) -> None:
        sent.append({"to": to, "subject": subject, "html": html})

    monkeypatch.setattr("sutr.api.orgs.send_email", _capture)
    return sent


@pytest.mark.anyio
async def test_invite_url_withheld_when_email_backend_configured(client, sent_emails, monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")

    resp = await client.post("/api/org/invitations", json={"email": "newbie@example.com"})
    assert resp.status_code == 201
    assert resp.json()["invite_url"] is None

    # The link went to the invitee's mailbox, and only there.
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "newbie@example.com"
    assert "/join?token=" in sent_emails[0]["html"]


@pytest.mark.anyio
async def test_invite_url_withheld_in_cloud_mode_even_without_email_key(
    client, sent_emails, monkeypatch
):
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "is_cloud", True)

    resp = await client.post("/api/org/invitations", json={"email": "newbie@example.com"})
    assert resp.status_code == 201
    assert resp.json()["invite_url"] is None


@pytest.mark.anyio
async def test_invite_url_returned_only_for_self_hosted_without_email(
    client, sent_emails, monkeypatch
):
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "is_cloud", False)

    resp = await client.post("/api/org/invitations", json={"email": "newbie@example.com"})
    assert resp.status_code == 201
    assert "/join?token=" in resp.json()["invite_url"]


@pytest.mark.anyio
async def test_account_from_emailed_invite_starts_verified(
    client, session, sent_emails, monkeypatch
):
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    await client.post("/api/org/invitations", json={"email": "newbie@example.com"})
    token = sent_emails[0]["html"].split("token=")[1].split('"')[0]

    resp = await client.post(
        "/api/org-invitations/accept", json={"token": token, "password": "hunter22"}
    )
    assert resp.status_code == 200
    user = session.exec(select(User).where(User.email == "newbie@example.com")).first()
    assert user is not None
    assert user.email_verified is True


@pytest.mark.anyio
async def test_account_from_out_of_band_invite_starts_unverified(client, session, monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "is_cloud", False)
    resp = await client.post("/api/org/invitations", json={"email": "newbie@example.com"})
    token = resp.json()["invite_url"].split("token=")[1]

    resp = await client.post(
        "/api/org-invitations/accept", json={"token": token, "password": "hunter22"}
    )
    assert resp.status_code == 200
    user = session.exec(select(User).where(User.email == "newbie@example.com")).first()
    assert user is not None
    # The inviter saw this token too, so it proves nothing about the mailbox.
    assert user.email_verified is False
