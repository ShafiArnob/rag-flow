"""Stores, reads and replaces secrets, each sealed under a data key of its own that the key
provider wraps.

Every method runs in the caller's ``workspace_transaction``, so row-level security confines it to
that workspace, and a secret is written in the same transaction as the row that refers to it.
Plaintext appears only in the caller's SecretStr and is never logged or put in an error.
"""

import uuid
from typing import Any

from cryptography.exceptions import InvalidTag
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.crypto.envelope import SealedSecret, last4, new_data_key, open_sealed, seal, secret_aad
from app.crypto.key_provider import KeyProvider, KeyUnwrapError

_INSERT_SECRET = text(
    "INSERT INTO secrets (id, workspace_id, ciphertext, wrapped_dek, key_id, nonce, last4)"
    " VALUES (:id, :workspace_id, :ciphertext, :wrapped_dek, :key_id, :nonce, :last4)"
)
_SELECT_SECRET = text(
    "SELECT ciphertext, wrapped_dek, key_id, nonce FROM secrets"
    " WHERE id = :id AND workspace_id = :workspace_id"
)
_REPLACE_SECRET = text(
    "UPDATE secrets SET ciphertext = :ciphertext, wrapped_dek = :wrapped_dek, key_id = :key_id,"
    " nonce = :nonce, last4 = :last4, updated_at = now()"
    " WHERE id = :id AND workspace_id = :workspace_id"
)


class SecretNotFoundError(Exception):
    def __init__(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> None:
        super().__init__(f"workspace {workspace_id} has no secret {secret_id}")
        self.workspace_id = workspace_id
        self.secret_id = secret_id


class SecretDecryptionError(Exception):
    def __init__(self, workspace_id: uuid.UUID, secret_id: uuid.UUID) -> None:
        super().__init__(
            f"secret {secret_id} in workspace {workspace_id} failed authentication; its stored"
            " value was altered or belongs to another secret"
        )
        self.workspace_id = workspace_id
        self.secret_id = secret_id


class SecretService:
    def __init__(self, key_provider: KeyProvider) -> None:
        self._key_provider = key_provider

    async def put(
        self, connection: AsyncConnection, *, workspace_id: uuid.UUID, plaintext: SecretStr
    ) -> uuid.UUID:
        """Stores ``plaintext`` as a new secret of ``workspace_id`` and returns its id."""
        secret_id = uuid.uuid4()
        sealed_columns = await self._sealed_columns(workspace_id, secret_id, plaintext)
        await connection.execute(
            _INSERT_SECRET, {"id": secret_id, "workspace_id": workspace_id, **sealed_columns}
        )
        return secret_id

    async def get(
        self, connection: AsyncConnection, *, workspace_id: uuid.UUID, secret_id: uuid.UUID
    ) -> SecretStr:
        """Raises SecretNotFoundError if the workspace has no such secret, and
        SecretDecryptionError if its wrapped data key or its sealed value fails authentication:
        either was altered, or was copied from another secret.

        The key provider's UnknownKeyIdError propagates: the keyring lacks the key that wrapped the
        data key, which is a misconfigured deployment rather than a bad row.
        """
        row = (
            await connection.execute(
                _SELECT_SECRET, {"id": secret_id, "workspace_id": workspace_id}
            )
        ).one_or_none()
        if row is None:
            raise SecretNotFoundError(workspace_id, secret_id)
        aad = secret_aad(workspace_id, secret_id)
        try:
            data_key = await self._key_provider.unwrap(row.wrapped_dek, row.key_id, aad)
        except KeyUnwrapError as error:
            raise SecretDecryptionError(workspace_id, secret_id) from error
        sealed = SealedSecret(ciphertext=row.ciphertext, nonce=row.nonce)
        try:
            plaintext = open_sealed(data_key, sealed, aad)
        except InvalidTag:
            raise SecretDecryptionError(workspace_id, secret_id) from None
        return SecretStr(plaintext.decode())

    async def rotate(
        self,
        connection: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        secret_id: uuid.UUID,
        new_plaintext: SecretStr,
    ) -> None:
        """Replaces the value of the secret under the same id, sealed under a new data key, so
        whatever refers to the secret now reads ``new_plaintext``. Raises SecretNotFoundError if the
        workspace has no such secret."""
        sealed_columns = await self._sealed_columns(workspace_id, secret_id, new_plaintext)
        result = await connection.execute(
            _REPLACE_SECRET, {"id": secret_id, "workspace_id": workspace_id, **sealed_columns}
        )
        if result.rowcount == 0:
            raise SecretNotFoundError(workspace_id, secret_id)

    async def _sealed_columns(
        self, workspace_id: uuid.UUID, secret_id: uuid.UUID, plaintext: SecretStr
    ) -> dict[str, Any]:
        value = plaintext.get_secret_value()
        aad = secret_aad(workspace_id, secret_id)
        data_key = new_data_key()
        sealed = seal(data_key, value.encode(), aad)
        wrapped = await self._key_provider.wrap(data_key, aad)
        return {
            "ciphertext": sealed.ciphertext,
            "wrapped_dek": wrapped.wrapped,
            "key_id": wrapped.key_id,
            "nonce": sealed.nonce,
            "last4": last4(value),
        }
