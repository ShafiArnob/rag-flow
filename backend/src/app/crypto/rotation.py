"""Re-wraps every secret's data key under the key provider's active master key, so a retired master
key can be dropped from the keyring.

Only ``wrapped_dek`` and ``key_id`` change: the data keys, and so the ciphertexts, nonces and
``updated_at``, stay as they are. It runs as the schema owner with row security on; the
secrets_key_rotation_* policies of migration 0006 let it read and update every workspace's rows of
``secrets``. Those policies limit rows, not columns, so it is this module's own queries that keep
rotation to ``wrapped_dek`` and ``key_id``.

Each batch commits on its own, so an interrupted run keeps the batches it finished and a rerun
carries on: finished rows no longer match ``key_id <> active``.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.crypto.envelope import secret_aad
from app.crypto.key_provider import KeyProvider, KeyUnwrapError, UnknownKeyIdError

REWRAP_BATCH_SIZE = 100

# FOR UPDATE, so a secret replaced concurrently is re-wrapped after that write, not over it.
_SECRETS_UNDER_OTHER_KEYS = text(
    "SELECT id, workspace_id, wrapped_dek, key_id FROM secrets"
    " WHERE key_id <> :active_key_id ORDER BY id LIMIT :batch_size FOR UPDATE"
)
_REPLACE_WRAPPED_DATA_KEY = text(
    "UPDATE secrets SET wrapped_dek = :wrapped_dek, key_id = :key_id WHERE id = :id"
)
_COUNT_SECRETS_UNDER_OTHER_KEYS = text(
    "SELECT count(*) FROM secrets WHERE key_id <> :active_key_id"
)


@dataclass(frozen=True)
class RewrapReport:
    active_key_id: str
    rewrapped: int
    # Secrets still under another key once the run ends: written meanwhile by an API instance that
    # wraps under an older keyring.
    remaining: int


class DataKeyRewrapError(Exception):
    def __init__(self, secret_id: uuid.UUID, workspace_id: uuid.UUID, key_id: str) -> None:
        super().__init__(
            f"cannot re-wrap the data key of secret {secret_id} in workspace {workspace_id}:"
            f" it is recorded as wrapped under {key_id!r}, and no key in the keyring unwraps it"
        )
        self.secret_id = secret_id
        self.workspace_id = workspace_id
        self.key_id = key_id


class KeyProviderContractError(Exception):
    def __init__(self, active_key_id: str, wrapped_key_id: str) -> None:
        super().__init__(
            f"the key provider broke its contract: its active key is {active_key_id!r}, but it"
            f" wrapped a data key under {wrapped_key_id!r}"
        )
        self.active_key_id = active_key_id
        self.wrapped_key_id = wrapped_key_id


async def rewrap_data_keys(
    engine: AsyncEngine, key_provider: KeyProvider, *, batch_size: int = REWRAP_BATCH_SIZE
) -> RewrapReport:
    """Re-wraps, in batches of ``batch_size``, every data key not under the active master key.

    Raises DataKeyRewrapError, naming the secret, if a data key cannot be unwrapped, and
    KeyProviderContractError if the provider wraps under a key other than its active one; the
    batches before either stay committed.
    """
    active_key_id = key_provider.active_key_id
    rewrapped = 0
    while True:
        async with engine.begin() as connection:
            batch = (
                await connection.execute(
                    _SECRETS_UNDER_OTHER_KEYS,
                    {"active_key_id": active_key_id, "batch_size": batch_size},
                )
            ).all()
            for secret in batch:
                context = secret_aad(secret.workspace_id, secret.id)
                try:
                    data_key = await key_provider.unwrap(secret.wrapped_dek, secret.key_id, context)
                except (UnknownKeyIdError, KeyUnwrapError) as error:
                    raise DataKeyRewrapError(
                        secret.id, secret.workspace_id, secret.key_id
                    ) from error
                wrapped = await key_provider.wrap(data_key, context)
                # Otherwise the row would still match key_id <> active, and every batch would
                # re-wrap it again without end.
                if wrapped.key_id != active_key_id:
                    raise KeyProviderContractError(active_key_id, wrapped.key_id)
                await connection.execute(
                    _REPLACE_WRAPPED_DATA_KEY,
                    {"id": secret.id, "wrapped_dek": wrapped.wrapped, "key_id": wrapped.key_id},
                )
        if not batch:
            break
        rewrapped += len(batch)
    async with engine.connect() as connection:
        remaining = (
            await connection.execute(
                _COUNT_SECRETS_UNDER_OTHER_KEYS, {"active_key_id": active_key_id}
            )
        ).scalar_one()
    return RewrapReport(active_key_id=active_key_id, rewrapped=rewrapped, remaining=remaining)
