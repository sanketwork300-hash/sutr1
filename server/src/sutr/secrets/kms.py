import base64
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sutr.models.secret import Secret
from sutr.secrets.base import SecretsBackend, StoredSecret

logger = logging.getLogger(__name__)

# Rows written before the ref was bound in as associated data decrypt only
# with no AAD. Noted once per process: it is an upgrade artefact, not a fault.
_legacy_aad_warned = False


def _aad(ref: str | None) -> bytes:
    """The row's reference, bound into the ciphertext.

    Without it, someone who can write to the database could move org B's
    ciphertext and wrapped key onto org A's row and have A's next call sent
    with B's credential. With it, the ciphertext verifies only on the row it
    was written for.
    """
    return (ref or "").encode("utf-8")


class DBKMSSecretsBackend(SecretsBackend):
    """Store ciphertext in the database and protect data keys with AWS KMS."""

    def __init__(self, key_id: str, region: str | None = None) -> None:
        if not key_id:
            raise RuntimeError("SECRETS_KMS_KEY_ID is required when SECRETS_BACKEND=db_kms")
        try:
            import boto3
        except ImportError:
            raise RuntimeError(
                "boto3 is required for the db_kms secrets backend. "
                "Install it with: uv sync --extra aws"
            )
        kwargs: dict = {}
        if region:
            kwargs["region_name"] = region
        self._client = boto3.client("kms", **kwargs)
        self._key_id = key_id

    def store(self, ref: str, value: str) -> StoredSecret:
        response = self._client.generate_data_key(KeyId=self._key_id, KeySpec="AES_256")
        plaintext_key = response["Plaintext"]
        encrypted_data_key = base64.b64encode(response["CiphertextBlob"]).decode()
        nonce = os.urandom(12)
        ciphertext = AESGCM(plaintext_key).encrypt(nonce, value.encode(), _aad(ref))
        payload = base64.b64encode(nonce + ciphertext).decode()
        return StoredSecret(
            value=payload,
            encrypted_data_key=encrypted_data_key,
            kms_key_id=self._key_id,
        )

    def retrieve(self, secret: Secret) -> str | None:
        if not secret.value or not secret.encrypted_data_key:
            return None
        response = self._client.decrypt(CiphertextBlob=base64.b64decode(secret.encrypted_data_key))
        plaintext_key = response["Plaintext"]
        payload = base64.b64decode(secret.value)
        nonce = payload[:12]
        ciphertext = payload[12:]
        aesgcm = AESGCM(plaintext_key)
        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, _aad(secret.ref))
        except InvalidTag:
            # Written before the ref was bound in. Readable, so the upgrade
            # does not break stored credentials; rewritten with AAD the next
            # time the row is stored.
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            global _legacy_aad_warned
            if not _legacy_aad_warned:
                _legacy_aad_warned = True
                logger.warning(
                    "secrets: KMS row %s was encrypted without associated data; "
                    "re-store it to bind the ciphertext to its reference",
                    secret.id,
                )
        return plaintext.decode()

    def delete(self, secret: Secret) -> None:
        pass
