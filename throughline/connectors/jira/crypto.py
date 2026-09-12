"""Encrypt/decrypt per-org connector credentials (Fernet).

Never log plaintext tokens. The key comes from ``CREDENTIALS_ENCRYPTION_KEY``.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from throughline.config import settings

# Documented insecure development default only — never use in production.
# base64.urlsafe_b64encode(b"throughline-dev-fernet-key-32b!!")
_DEV_FERNET_KEY = b"dGhyb3VnaGxpbmUtZGV2LWZlcm5ldC1rZXktMzJiISE="


class CredentialsEncryptionError(RuntimeError):
    """Raised when credentials cannot be encrypted or decrypted."""


def _fernet() -> Fernet:
    raw = settings.credentials_encryption_key.strip()
    if not raw:
        if settings.is_production:
            raise CredentialsEncryptionError(
                "CREDENTIALS_ENCRYPTION_KEY is required in production"
            )
        raw = _DEV_FERNET_KEY.decode("ascii")
    try:
        return Fernet(raw.encode("ascii") if isinstance(raw, str) else raw)
    except (ValueError, TypeError) as exc:
        raise CredentialsEncryptionError(
            "CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def encrypt_secret(plaintext: str) -> bytes:
    """Encrypt a secret string to Fernet ciphertext bytes."""
    if not plaintext:
        raise CredentialsEncryptionError("Refusing to encrypt empty secret")
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes) -> str:
    """Decrypt Fernet ciphertext to a UTF-8 string."""
    if not ciphertext:
        raise CredentialsEncryptionError("Refusing to decrypt empty ciphertext")
    try:
        return _fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialsEncryptionError("Failed to decrypt credential") from exc
