"""Key providers wrap each secret's data key under a master key that never leaves the provider.

The local provider holds a keyring: an ordered list of master keys, read from MASTER_KEYS or
MASTER_KEYS_FILE. The first key wraps; every key unwraps, so data keys wrapped under a retired key
stay readable until ``flowbuilder keys rotate`` re-wraps them. docs/key-providers.md has the
runbook and the designs for Vault and AWS KMS.

No error, log record or repr here contains key material; they name key ids, sources and lines.
"""

import base64
import hashlib
import hmac
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import KeyringSettings

MASTER_KEY_BYTES = 32
# Rejects placeholders and patterned keys such as all zeros; 32 random bytes have about 30
# distinct values, and fewer than 16 is vanishingly unlikely.
MASTER_KEY_MINIMUM_DISTINCT_BYTES = 16
# A key made only of these bytes is a passphrase typed or pasted in, not 32 random bytes.
PRINTABLE_ASCII = range(0x20, 0x7F)
LOCAL_KEY_ID_PREFIX = "local:"
LOCAL_KEY_ID_HASH_CHARACTERS = 16
# The id is an HMAC under the key with this fixed label, not a plain hash of the key, so it is
# bound to this one use and stores no digest of the key that another system might also compute.
LOCAL_KEY_ID_LABEL = b"flowbuilder/key-id"
WRAP_NONCE_BYTES = 12
GCM_TAG_BYTES = 16
CONTEXT_LENGTH_BYTES = 2


@dataclass(frozen=True)
class WrappedDataKey:
    wrapped: bytes
    key_id: str


class KeyProvider(Protocol):
    """Wraps and unwraps data keys. Async, so providers that call a KMS over the network fit.

    ``context`` names what a data key belongs to; the secret service passes
    ``secret_aad(workspace_id, secret_id)``. A data key unwraps only with the context it was
    wrapped with, so a wrapped key moved to another secret or workspace is refused by the provider
    itself.
    """

    @property
    def active_key_id(self) -> str:
        """The id of the master key that ``wrap`` uses."""
        ...

    async def wrap(self, data_key: bytes, context: bytes) -> WrappedDataKey:
        """Wraps under the active key: the result's key_id is always ``active_key_id``."""
        ...

    async def unwrap(self, wrapped: bytes, key_id: str, context: bytes) -> bytes:
        """Raises UnknownKeyIdError if the provider has no key ``key_id``, and KeyUnwrapError if
        ``wrapped`` was not wrapped by it for ``context``."""
        ...


class KeyringError(Exception):
    """The keyring is missing, unreadable or holds a weak key. The message names the source and
    line, never a key."""


class UnknownKeyIdError(Exception):
    def __init__(self, key_id: str) -> None:
        super().__init__(f"no master key with id {key_id!r} is in the keyring")
        self.key_id = key_id


class KeyUnwrapError(Exception):
    def __init__(self, key_id: str) -> None:
        super().__init__(
            f"a data key wrapped under {key_id!r} failed authentication; it was altered, or"
            " wrapped under another key or for another secret"
        )
        self.key_id = key_id


def local_key_id(master_key: bytes) -> str:
    """Identifies a master key without revealing it, so each wrapped data key records which key
    unwraps it."""
    digest = hmac.new(master_key, LOCAL_KEY_ID_LABEL, hashlib.sha256).hexdigest()
    return LOCAL_KEY_ID_PREFIX + digest[:LOCAL_KEY_ID_HASH_CHARACTERS]


def _wrap_aad(context: bytes, key_id: str) -> bytes:
    """The context, prefixed with its length so no context and key id pair can be mistaken for
    another, then the key id."""
    return len(context).to_bytes(CONTEXT_LENGTH_BYTES, "big") + context + key_id.encode()


class LocalKeyProvider:
    """Wraps data keys with AES-256-GCM under the first master key, with the context and key id as
    associated data, and unwraps them under any key of the keyring. A wrapped key is
    nonce || ciphertext+tag.

    Takes the keys as parse_master_keys returns them: strong, distinct, in keyring order.
    """

    def __init__(self, master_keys: Sequence[bytes]) -> None:
        if not master_keys:
            raise KeyringError("a keyring needs at least one master key")
        # Only the ciphers are kept; they do not expose their keys.
        self._ciphers = {local_key_id(key): AESGCM(key) for key in master_keys}
        self._active_key_id = local_key_id(master_keys[0])

    def __repr__(self) -> str:
        return f"LocalKeyProvider(key_ids={list(self._ciphers)!r})"

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    async def wrap(self, data_key: bytes, context: bytes) -> WrappedDataKey:
        nonce = os.urandom(WRAP_NONCE_BYTES)
        cipher = self._ciphers[self._active_key_id]
        aad = _wrap_aad(context, self._active_key_id)
        wrapped = nonce + cipher.encrypt(nonce, data_key, aad)
        return WrappedDataKey(wrapped=wrapped, key_id=self._active_key_id)

    async def unwrap(self, wrapped: bytes, key_id: str, context: bytes) -> bytes:
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise UnknownKeyIdError(key_id)
        # The blob comes from the database, so its length is not to be trusted.
        if len(wrapped) < WRAP_NONCE_BYTES + GCM_TAG_BYTES:
            raise KeyUnwrapError(key_id)
        nonce, ciphertext = wrapped[:WRAP_NONCE_BYTES], wrapped[WRAP_NONCE_BYTES:]
        try:
            return cipher.decrypt(nonce, ciphertext, _wrap_aad(context, key_id))
        except InvalidTag:
            raise KeyUnwrapError(key_id) from None


def parse_master_keys(text: str, source: str) -> list[bytes]:
    """The master keys in ``text``, in order: base64 values separated by any whitespace, so one
    environment variable can hold several. ``source`` names where the text came from in errors.

    Raises KeyringError if the text holds no key, a key that is not base64 or does not decode to
    MASTER_KEY_BYTES bytes, a key with fewer than MASTER_KEY_MINIMUM_DISTINCT_BYTES distinct byte
    values or made only of printable ASCII, or the same key twice.
    """
    keys: list[bytes] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for token in line.split():
            where = f"{source}: key {len(keys) + 1}, on line {line_number},"
            # binascii.Error for bad base64; a plain ValueError for text that is not ASCII.
            try:
                key = base64.b64decode(token, validate=True)
            except ValueError:
                raise KeyringError(f"{where} is not valid base64") from None
            if len(key) != MASTER_KEY_BYTES:
                raise KeyringError(
                    f"{where} decodes to {len(key)} bytes, not {MASTER_KEY_BYTES};"
                    f" generate one with: openssl rand -base64 {MASTER_KEY_BYTES}"
                )
            if len(set(key)) < MASTER_KEY_MINIMUM_DISTINCT_BYTES:
                raise KeyringError(
                    f"{where} has fewer than {MASTER_KEY_MINIMUM_DISTINCT_BYTES} distinct byte"
                    f" values, so it is a placeholder or not random; generate one with:"
                    f" openssl rand -base64 {MASTER_KEY_BYTES}"
                )
            if all(byte in PRINTABLE_ASCII for byte in key):
                raise KeyringError(
                    f"{where} is printable text, so it is a passphrase rather than random bytes;"
                    f" generate one with: openssl rand -base64 {MASTER_KEY_BYTES}"
                )
            if key in keys:
                raise KeyringError(f"{where} repeats key {keys.index(key) + 1}")
            keys.append(key)
    if not keys:
        raise KeyringError(f"{source} holds no master key")
    return keys


def load_local_key_provider(settings: KeyringSettings) -> LocalKeyProvider:
    """The local provider over the keyring that ``settings`` points to. Raises KeyringError if the
    keyring cannot be read or is invalid."""
    if settings.master_keys is not None:
        return LocalKeyProvider(
            parse_master_keys(settings.master_keys.get_secret_value(), "MASTER_KEYS")
        )
    path = settings.master_keys_file
    if path is None:
        raise KeyringError("set exactly one of MASTER_KEYS and MASTER_KEYS_FILE")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise KeyringError(f"cannot read MASTER_KEYS_FILE {path}: {error.strerror}") from None
    # The decoding error quotes the byte it failed on, which may be key material.
    except UnicodeDecodeError:
        raise KeyringError(f"MASTER_KEYS_FILE {path} is not UTF-8 text") from None
    return LocalKeyProvider(parse_master_keys(text, f"MASTER_KEYS_FILE {path}"))
