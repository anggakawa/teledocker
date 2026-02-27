"""AES-256-GCM helpers for encrypting and decrypting API keys at rest.

Why AES-256-GCM:
- Authenticated encryption — any tampering with ciphertext is detected.
- The `cryptography` AESGCM class appends the 16-byte auth tag to the ciphertext
  automatically, so callers never have to manage the tag separately.
- IV (nonce) must be unique per encryption; we generate 12 random bytes each time.
"""

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256 requires exactly 32 bytes for the key.
_REQUIRED_KEY_LENGTH = 32


def encrypt_api_key(plaintext: str, master_key: bytes) -> tuple[bytes, bytes]:
    """Encrypt a plaintext API key and return (ciphertext_with_tag, iv).

    Args:
        plaintext: The API key string to encrypt.
        master_key: 32-byte AES-256 encryption key from environment config.

    Returns:
        A tuple of (ciphertext_with_tag, iv). Both must be stored to decrypt later.
    """
    if len(master_key) != _REQUIRED_KEY_LENGTH:
        raise ValueError(f"master_key must be exactly {_REQUIRED_KEY_LENGTH} bytes")

    iv = os.urandom(12)  # 96-bit nonce — NIST recommended size for GCM
    aesgcm = AESGCM(master_key)
    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    return ciphertext_with_tag, iv


def decrypt_api_key(ciphertext: bytes, iv: bytes, master_key: bytes) -> str:
    """Decrypt a previously encrypted API key back to plaintext.

    Args:
        ciphertext: The ciphertext including the 16-byte GCM authentication tag.
        iv: The initialization vector used during encryption.
        master_key: 32-byte AES-256 encryption key from environment config.

    Returns:
        The original plaintext API key string.

    Raises:
        cryptography.exceptions.InvalidTag: If the ciphertext was tampered with.
    """
    if len(master_key) != _REQUIRED_KEY_LENGTH:
        raise ValueError(f"master_key must be exactly {_REQUIRED_KEY_LENGTH} bytes")

    aesgcm = AESGCM(master_key)
    plaintext_bytes = aesgcm.decrypt(iv, ciphertext, None)
    return plaintext_bytes.decode("utf-8")
