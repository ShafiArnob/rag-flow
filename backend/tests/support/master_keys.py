"""Master keys and keyrings for the crypto tests."""

import base64
import os

from app.crypto.key_provider import MASTER_KEY_BYTES, LocalKeyProvider

# What .env.example ships with; it must be refused, so a copied example never protects anything.
ALL_ZERO_PLACEHOLDER = base64.b64encode(bytes(MASTER_KEY_BYTES)).decode()

LEFT_DOUBLE_QUOTE = chr(0x201C)
RIGHT_DOUBLE_QUOTE = chr(0x201D)


def new_master_key() -> bytes:
    return os.urandom(MASTER_KEY_BYTES)


def encoded(*keys: bytes) -> str:
    """The keys as a MASTER_KEYS value: base64, separated by spaces."""
    return " ".join(base64.b64encode(key).decode() for key in keys)


def in_typographic_quotes(value: str) -> str:
    """``value`` between curly quotes, as a key pasted from a document can arrive. b64decode
    rejects such non-ASCII text with a plain ValueError rather than binascii.Error."""
    return LEFT_DOUBLE_QUOTE + value + RIGHT_DOUBLE_QUOTE


def provider_with(*keys: bytes) -> LocalKeyProvider:
    return LocalKeyProvider(list(keys))
