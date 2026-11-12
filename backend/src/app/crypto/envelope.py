"""Envelope encryption of one secret: AES-256-GCM under a data key of its own.

The associated data binds the ciphertext to its row, so ciphertext copied onto another secret, or
into another workspace, fails to decrypt instead of passing for that secret's value.
"""

import os
import uuid
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DATA_KEY_BYTES = 32
NONCE_BYTES = 12
# A shorter value would give away too much of itself in its last four characters.
LAST4_MINIMUM_LENGTH = 12
LAST4_LENGTH = 4


@dataclass(frozen=True)
class SealedSecret:
    ciphertext: bytes
    nonce: bytes


def new_data_key() -> bytes:
    return AESGCM.generate_key(bit_length=DATA_KEY_BYTES * 8)


def secret_aad(workspace_id: uuid.UUID, secret_id: uuid.UUID) -> bytes:
    """The associated data of the secret ``secret_id`` in ``workspace_id``."""
    return workspace_id.bytes + secret_id.bytes


def seal(data_key: bytes, plaintext: bytes, aad: bytes) -> SealedSecret:
    """Encrypts under a fresh random nonce: GCM loses its guarantees if a nonce repeats under a
    key."""
    nonce = os.urandom(NONCE_BYTES)
    return SealedSecret(ciphertext=AESGCM(data_key).encrypt(nonce, plaintext, aad), nonce=nonce)


def open_sealed(data_key: bytes, sealed: SealedSecret, aad: bytes) -> bytes:
    """Raises cryptography.exceptions.InvalidTag if the key, nonce, ciphertext or ``aad`` differ
    from the ones it was sealed with."""
    return AESGCM(data_key).decrypt(sealed.nonce, sealed.ciphertext, aad)


def last4(plaintext: str) -> str:
    """The hint shown in place of a secret: its last four characters, or '' when it is shorter than
    LAST4_MINIMUM_LENGTH."""
    if len(plaintext) < LAST4_MINIMUM_LENGTH:
        return ""
    return plaintext[-LAST4_LENGTH:]
