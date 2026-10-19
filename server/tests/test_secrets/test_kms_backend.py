import base64
import sys
from unittest.mock import MagicMock, patch

import pytest

from sutr.models.secret import Secret
from sutr.secrets.kms import DBKMSSecretsBackend


@pytest.fixture()
def kms_backend():
    mock_client = MagicMock()
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        backend = DBKMSSecretsBackend(key_id="kms-key", region="us-east-1")

    yield backend, mock_client


def test_store_encrypts_secret(kms_backend):
    backend, client = kms_backend
    plaintext_key = b"k" * 32
    client.generate_data_key.return_value = {
        "Plaintext": plaintext_key,
        "CiphertextBlob": b"encrypted-key",
    }

    stored = backend.store("my/key", "secret-value")

    client.generate_data_key.assert_called_once_with(KeyId="kms-key", KeySpec="AES_256")
    assert stored.value is not None
    assert stored.encrypted_data_key == base64.b64encode(b"encrypted-key").decode()
    assert stored.kms_key_id == "kms-key"


def test_retrieve_decrypts_secret(kms_backend):
    backend, client = kms_backend
    plaintext_key = b"k" * 32
    client.generate_data_key.return_value = {
        "Plaintext": plaintext_key,
        "CiphertextBlob": b"encrypted-key",
    }
    stored = backend.store("my/key", "secret-value")
    client.decrypt.return_value = {"Plaintext": plaintext_key}
    secret = Secret(
        kind="test",
        storage_backend="db_kms",
        ref="my/key",
        value=stored.value,
        encrypted_data_key=stored.encrypted_data_key,
        kms_key_id="kms-key",
    )

    result = backend.retrieve(secret)

    assert result == "secret-value"
    client.decrypt.assert_called_once_with(CiphertextBlob=b"encrypted-key")


def test_retrieve_none_without_payload(kms_backend):
    backend, _ = kms_backend
    secret = Secret(
        kind="test",
        storage_backend="db_kms",
        value=None,
        encrypted_data_key=None,
        kms_key_id="kms-key",
    )

    assert backend.retrieve(secret) is None


def test_import_error_without_boto3():
    with patch.dict(sys.modules, {"boto3": None}):
        with pytest.raises(RuntimeError, match="boto3 is required"):
            DBKMSSecretsBackend(key_id="kms-key")


# ── Associated data ──────────────────────────────────────────────────────────


def _kms_row(stored, ref: str) -> Secret:
    return Secret(
        kind="test",
        storage_backend="db_kms",
        ref=ref,
        value=stored.value,
        encrypted_data_key=stored.encrypted_data_key,
        kms_key_id="kms-key",
    )


def test_the_ciphertext_is_bound_to_its_ref(kms_backend):
    """A ciphertext and wrapped key transplanted onto another row do not verify."""
    from cryptography.exceptions import InvalidTag

    backend, client = kms_backend
    plaintext_key = b"k" * 32
    client.generate_data_key.return_value = {"Plaintext": plaintext_key, "CiphertextBlob": b"ek"}
    client.decrypt.return_value = {"Plaintext": plaintext_key}
    stored = backend.store("credentials/org-b/stripe/apiKey", "sk_b")

    assert backend.retrieve(_kms_row(stored, "credentials/org-b/stripe/apiKey")) == "sk_b"
    with pytest.raises(InvalidTag):
        backend.retrieve(_kms_row(stored, "credentials/org-a/stripe/apiKey"))


def test_a_row_encrypted_without_aad_is_still_readable(kms_backend, monkeypatch):
    """Rows written before the ref was bound in decrypt with no AAD, with one warning."""
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from sutr.secrets import kms as kms_module

    backend, client = kms_backend
    plaintext_key = b"k" * 32
    client.decrypt.return_value = {"Plaintext": plaintext_key}
    nonce = os.urandom(12)
    legacy_payload = base64.b64encode(
        nonce + AESGCM(plaintext_key).encrypt(nonce, b"old-token", None)
    ).decode()
    row = Secret(
        kind="test",
        storage_backend="db_kms",
        ref="integrations/org/github/token",
        value=legacy_payload,
        encrypted_data_key=base64.b64encode(b"ek").decode(),
        kms_key_id="kms-key",
    )

    # Recorded directly rather than through caplog: the structured-logging
    # tests reconfigure the sutr logger, so capture depends on test order.
    warnings: list[str] = []
    monkeypatch.setattr(kms_module.logger, "warning", lambda msg, *a: warnings.append(msg % a))
    monkeypatch.setattr(kms_module, "_legacy_aad_warned", False)
    assert backend.retrieve(row) == "old-token"
    assert backend.retrieve(row) == "old-token"
    assert len(warnings) == 1 and "without associated data" in warnings[0]
