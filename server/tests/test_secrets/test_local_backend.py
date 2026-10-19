"""The default secrets backend: encrypted at rest, keyed from the environment."""

import base64
import uuid

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sutr import secrets as secrets_pkg
from sutr.config import settings
from sutr.models.secret import Secret
from sutr.secrets.local import (
    PREFIX,
    LocalEncryptedSecretsBackend,
    SecretDecryptionError,
    decode_key,
    derive_key,
)
from sutr.secrets.records import get_secret_value, reencrypt_legacy_secrets, upsert_secret

KEY = b"\x01" * 32


def _row(ref: str, stored, backend: str = "db_encrypted") -> Secret:
    return Secret(
        id=uuid.uuid4(),
        kind="test",
        storage_backend=backend,
        ref=ref,
        value=stored.value,
        encrypted_data_key=stored.encrypted_data_key,
        kms_key_id=stored.kms_key_id,
    )


def test_round_trip():
    backend = LocalEncryptedSecretsBackend(KEY)
    stored = backend.store("oauth/org/github/access", "gho_live_token")
    assert stored.value.startswith(PREFIX)
    assert "gho_live_token" not in stored.value
    assert stored.encrypted_data_key is None
    assert stored.kms_key_id is None
    assert backend.retrieve(_row("oauth/org/github/access", stored)) == "gho_live_token"


def test_each_store_uses_a_fresh_nonce():
    backend = LocalEncryptedSecretsBackend(KEY)
    first = backend.store("r", "same").value
    second = backend.store("r", "same").value
    assert first != second


def test_the_ciphertext_is_bound_to_its_ref():
    """A ciphertext moved onto another row (a different ref) does not verify."""
    backend = LocalEncryptedSecretsBackend(KEY)
    stored = backend.store("credentials/org-a/stripe/apiKey", "sk_a")
    with pytest.raises(SecretDecryptionError):
        backend.retrieve(_row("credentials/org-b/stripe/apiKey", stored))


def test_a_different_key_cannot_read_it():
    stored = LocalEncryptedSecretsBackend(KEY).store("r", "v")
    with pytest.raises(SecretDecryptionError, match="does not verify"):
        LocalEncryptedSecretsBackend(b"\x02" * 32).retrieve(_row("r", stored))


def test_the_format_is_prefix_nonce_ciphertext():
    backend = LocalEncryptedSecretsBackend(KEY)
    stored = backend.store("r", "v")
    payload = base64.b64decode(stored.value[len(PREFIX) :])
    nonce, ciphertext = payload[:12], payload[12:]
    assert AESGCM(KEY).decrypt(nonce, ciphertext, b"r") == b"v"


def test_a_legacy_plaintext_row_is_still_readable():
    """Rows the old `db` backend wrote carry no prefix and come back as-is."""
    backend = LocalEncryptedSecretsBackend(KEY)
    legacy = Secret(kind="test", storage_backend="db", ref="r", value="plain_old_token")
    assert backend.retrieve(legacy) == "plain_old_token"


def test_none_and_malformed_values():
    backend = LocalEncryptedSecretsBackend(KEY)
    assert backend.retrieve(Secret(kind="t", storage_backend="db_encrypted", value=None)) is None
    with pytest.raises(SecretDecryptionError, match="malformed"):
        backend.retrieve(Secret(kind="t", storage_backend="db_encrypted", value=PREFIX + "AAAA"))
    with pytest.raises(SecretDecryptionError, match="malformed"):
        backend.retrieve(Secret(kind="t", storage_backend="db_encrypted", value=PREFIX + "!!"))


def test_delete_is_a_noop():
    LocalEncryptedSecretsBackend(KEY).delete(Secret(kind="t", storage_backend="db_encrypted"))


# ── Key handling ─────────────────────────────────────────────────────────────


def test_key_accepts_hex_and_base64():
    assert decode_key("ab" * 32) == b"\xab" * 32
    assert decode_key(base64.b64encode(KEY).decode()) == KEY
    assert decode_key(base64.urlsafe_b64encode(b"\xfb" * 32).decode()) == b"\xfb" * 32


@pytest.mark.parametrize("bad", ["", "short", "ab" * 16, base64.b64encode(b"x" * 31).decode()])
def test_key_of_the_wrong_length_is_refused(bad):
    with pytest.raises(RuntimeError, match="32 bytes"):
        decode_key(bad)


def test_explicit_key_is_used_when_set(monkeypatch):
    monkeypatch.setattr(settings, "secrets_encryption_key", "cd" * 32)
    assert derive_key() == b"\xcd" * 32


def test_key_is_derived_from_the_jwt_secret_and_differs_from_it(monkeypatch):
    monkeypatch.setattr(settings, "secrets_encryption_key", "")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-jwt-secret-of-some-length")
    derived = derive_key()
    assert len(derived) == 32
    assert derived != b"a-jwt-secret-of-some-length".ljust(32, b"\0")
    monkeypatch.setattr(settings, "jwt_secret_key", "another")
    assert derive_key() != derived


def test_a_secret_from_a_different_jwt_key_does_not_decrypt(monkeypatch):
    """The derived key is per install: a dump moved under another JWT secret is opaque."""
    monkeypatch.setattr(settings, "secrets_encryption_key", "")
    monkeypatch.setattr(settings, "jwt_secret_key", "install-one")
    stored = LocalEncryptedSecretsBackend().store("r", "v")
    monkeypatch.setattr(settings, "jwt_secret_key", "install-two")
    with pytest.raises(SecretDecryptionError):
        LocalEncryptedSecretsBackend().retrieve(_row("r", stored))


# ── Factory ──────────────────────────────────────────────────────────────────


def test_the_default_backend_is_encrypted():
    assert settings.secrets_backend == "db_encrypted"
    assert isinstance(secrets_pkg.secrets_backend, LocalEncryptedSecretsBackend)


def test_build_selects_the_encrypted_backend(monkeypatch):
    monkeypatch.setattr(settings, "secrets_backend", "db_encrypted")
    assert isinstance(secrets_pkg._build_backend(), LocalEncryptedSecretsBackend)


def test_plaintext_backend_is_refused_on_cloud(monkeypatch):
    monkeypatch.setattr(settings, "secrets_backend", "db")
    monkeypatch.setattr(settings, "is_cloud", True)
    with pytest.raises(RuntimeError, match="plaintext"):
        secrets_pkg._build_backend()


def test_plaintext_backend_is_still_selectable_self_hosted(monkeypatch):
    from sutr.secrets.db import DBSecretsBackend

    monkeypatch.setattr(settings, "secrets_backend", "db")
    monkeypatch.setattr(settings, "is_cloud", False)
    assert isinstance(secrets_pkg._build_backend(), DBSecretsBackend)


def test_unknown_backend_names_the_supported_ones(monkeypatch):
    monkeypatch.setattr(settings, "secrets_backend", "nope")
    with pytest.raises(ValueError, match="db_encrypted"):
        secrets_pkg._build_backend()


def test_describe_reports_encryption_at_rest(monkeypatch):
    monkeypatch.setattr(settings, "secrets_backend", "db_encrypted")
    assert "AES-256-GCM" in secrets_pkg.describe()["at_rest"]


# ── Records: stored through the real default backend ─────────────────────────


def test_upsert_stores_ciphertext_and_reads_back_plaintext(session, test_org):
    secret = upsert_secret(
        session, org_id=test_org.id, kind="k", ref="integrations/x/token", value="tok_1"
    )
    session.commit()
    row = session.get(Secret, secret.id)
    assert row.storage_backend == "db_encrypted"
    assert row.value.startswith(PREFIX)
    assert "tok_1" not in row.value
    assert get_secret_value(session, secret.id) == "tok_1"


def test_reencrypt_sweep_converts_legacy_rows_and_is_idempotent(session, test_org):
    legacy = [
        Secret(org_id=test_org.id, kind="k", storage_backend="db", ref=f"legacy/{i}", value=f"v{i}")
        for i in range(3)
    ]
    empty = Secret(
        org_id=test_org.id, kind="k", storage_backend="db", ref="legacy/none", value=None
    )
    already = upsert_secret(session, org_id=test_org.id, kind="k", ref="new/one", value="fresh")
    session.add_all(legacy + [empty])
    session.commit()

    assert reencrypt_legacy_secrets(session, batch_size=2) == 3

    for i, row in enumerate(legacy):
        session.refresh(row)
        assert row.storage_backend == "db_encrypted"
        assert row.value.startswith(PREFIX)
        assert get_secret_value(session, row.id) == f"v{i}"
    session.refresh(already)
    assert get_secret_value(session, already.id) == "fresh"
    # Nothing to convert on a second pass; the empty row is left alone.
    assert reencrypt_legacy_secrets(session) == 0
    session.refresh(empty)
    assert empty.storage_backend == "db"


def test_reencrypt_sweep_is_a_noop_when_plaintext_is_the_chosen_backend(
    session, test_org, monkeypatch
):
    session.add(Secret(org_id=test_org.id, kind="k", storage_backend="db", ref="r", value="v"))
    session.commit()
    monkeypatch.setattr(settings, "secrets_backend", "db")
    assert reencrypt_legacy_secrets(session) == 0
