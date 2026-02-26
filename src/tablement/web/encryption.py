"""Fernet encryption for Resy passwords stored in the database.

Resy passwords are encrypted at rest using a server-side Fernet key.
The key lives in the FERNET_KEY environment variable and never leaves
the server. Passwords are decrypted only at snipe time (T-60s) to get
a fresh Resy auth token.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    """Get a Fernet instance using the server-side key."""
    key = os.environ.get("FERNET_KEY", "")
    if not key:
        raise RuntimeError(
            "FERNET_KEY environment variable is required. "
            "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_password(plaintext: str) -> str:
    """Encrypt a password. Returns a base64-encoded ciphertext string."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    """Decrypt a password. Returns the plaintext string."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()
