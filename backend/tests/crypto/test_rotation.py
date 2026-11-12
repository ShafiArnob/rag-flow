"""Re-wrapping every data key under a new master key, as the owner role with row security on, and
the secrets_key_rotation_* policies that let it."""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Row, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.cli import main
from app.crypto.key_provider import KeyProvider, LocalKeyProvider, WrappedDataKey, local_key_id
from app.crypto.rotation import (
    DataKeyRewrapError,
    KeyProviderContractError,
    RewrapReport,
    rewrap_data_keys,
)
from app.crypto.service import SecretService
from app.database.tenancy import workspace_transaction
from support.master_keys import encoded, new_master_key, provider_with
from support.tenants import SECRET_NONCE_BYTES, TwoTenants

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

SECRETS_PER_WORKSPACE = (3, 2)
SECRET_COUNT = sum(SECRETS_PER_WORKSPACE)

# support.tenants leaves secrets under the made-up key 'test-key', which no keyring unwraps.
REMOVE_EVERY_SECRET = text("TRUNCATE connectors, secrets")
EVERY_SECRET = text(
    "SELECT id, workspace_id, ciphertext, nonce, last4, updated_at, wrapped_dek, key_id"
    " FROM secrets ORDER BY id"
)
SECRETS_OF_WORKSPACES = text(
    "SELECT DISTINCT workspace_id FROM secrets WHERE workspace_id IN (:first, :second)"
)
INSERT_SECRET = text(
    "INSERT INTO secrets (workspace_id, ciphertext, wrapped_dek, key_id, nonce, last4)"
    " VALUES (:workspace_id, :sealed, :sealed, 'owner-key', :nonce, '')"
)
DELETE_SECRETS = text("DELETE FROM secrets WHERE workspace_id IN (:first, :second)")
COUNT_SECRETS = text("SELECT count(*) FROM secrets")
COUNT_CONNECTORS = text("SELECT count(*) FROM connectors")
LOCK_WAITS = text(
    "SELECT count(*) FROM pg_stat_activity"
    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
)
LOCK_WAIT_TIMEOUT_SECONDS = 10
LOCK_WAIT_POLL_SECONDS = 0.05
# Bounds a rotation that would otherwise never end, so the test fails instead of hanging.
ROTATION_TIMEOUT_SECONDS = 10
CLAIMED_KEY_ID = "local:claimed"


@dataclass(frozen=True)
class StoredSecrets:
    workspace_ids: dict[uuid.UUID, uuid.UUID]
    plaintexts: dict[uuid.UUID, str]


class InjectedWrapFailure(Exception):
    pass


class WrapFailsAfter:
    """A key provider that fails on wrap number ``successful_wraps + 1``, as a KMS outage would."""

    def __init__(self, provider: KeyProvider, successful_wraps: int) -> None:
        self._provider = provider
        self._wraps_left = successful_wraps

    @property
    def active_key_id(self) -> str:
        return self._provider.active_key_id

    async def wrap(self, data_key: bytes, context: bytes) -> WrappedDataKey:
        if self._wraps_left == 0:
            raise InjectedWrapFailure
        self._wraps_left -= 1
        return await self._provider.wrap(data_key, context)

    async def unwrap(self, wrapped: bytes, key_id: str, context: bytes) -> bytes:
        return await self._provider.unwrap(wrapped, key_id, context)


class WrapsUnderAnotherKey:
    """A key provider that claims CLAIMED_KEY_ID as its active key but wraps under another key,
    breaking the KeyProvider contract."""

    def __init__(self, provider: KeyProvider) -> None:
        self._provider = provider

    @property
    def active_key_id(self) -> str:
        return CLAIMED_KEY_ID

    async def wrap(self, data_key: bytes, context: bytes) -> WrappedDataKey:
        return await self._provider.wrap(data_key, context)

    async def unwrap(self, wrapped: bytes, key_id: str, context: bytes) -> bytes:
        return await self._provider.unwrap(wrapped, key_id, context)


async def lock_wait_seen_within(superuser_engine: AsyncEngine, timeout_seconds: float) -> bool:
    """Whether some backend of this database waits on a lock before ``timeout_seconds`` pass."""
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                async with superuser_engine.connect() as connection:
                    if (await connection.execute(LOCK_WAITS)).scalar_one() > 0:
                        return True
                await asyncio.sleep(LOCK_WAIT_POLL_SECONDS)
    except TimeoutError:
        return False


@pytest.fixture
def old_key() -> bytes:
    return new_master_key()


@pytest.fixture
def new_key() -> bytes:
    return new_master_key()


@pytest.fixture
async def only_these_tenants(two_tenants: TwoTenants, superuser_engine: AsyncEngine) -> TwoTenants:
    """The two tenants with every secret in the database removed, so a rotation sees only the
    secrets a test stores."""
    async with superuser_engine.begin() as connection:
        await connection.execute(REMOVE_EVERY_SECRET)
    return two_tenants


@pytest.fixture
async def stored_secrets(
    app_engine: AsyncEngine, only_these_tenants: TwoTenants, old_key: bytes
) -> StoredSecrets:
    """SECRET_COUNT secrets across the two workspaces, stored under ``old_key``."""
    service = SecretService(provider_with(old_key))
    workspace_ids: dict[uuid.UUID, uuid.UUID] = {}
    plaintexts: dict[uuid.UUID, str] = {}
    tenants = (only_these_tenants.first, only_these_tenants.second)
    for tenant, count in zip(tenants, SECRETS_PER_WORKSPACE, strict=True):
        for index in range(count):
            plaintext = f"sk-live-{tenant.workspace_id}-{index}"
            async with workspace_transaction(app_engine, tenant.workspace_id) as connection:
                secret_id = await service.put(
                    connection, workspace_id=tenant.workspace_id, plaintext=SecretStr(plaintext)
                )
            workspace_ids[secret_id] = tenant.workspace_id
            plaintexts[secret_id] = plaintext
    return StoredSecrets(workspace_ids=workspace_ids, plaintexts=plaintexts)


async def every_secret(owner_engine: AsyncEngine) -> list[Row[Any]]:
    async with owner_engine.connect() as connection:
        return list((await connection.execute(EVERY_SECRET)).all())


def sealed_values(rows: list[Row[Any]]) -> list[tuple[object, ...]]:
    """What re-wrapping must leave alone: everything but wrapped_dek and key_id."""
    return [
        (row.id, row.workspace_id, row.ciphertext, row.nonce, row.last4, row.updated_at)
        for row in rows
    ]


async def assert_every_secret_decrypts(
    app_engine: AsyncEngine, provider: LocalKeyProvider, secrets: StoredSecrets
) -> None:
    service = SecretService(provider)
    for secret_id, workspace_id in secrets.workspace_ids.items():
        async with workspace_transaction(app_engine, workspace_id) as connection:
            value = await service.get(connection, workspace_id=workspace_id, secret_id=secret_id)
        assert value.get_secret_value() == secrets.plaintexts[secret_id]


async def test_rotation_rewraps_every_data_key_and_leaves_the_ciphertext_alone(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    stored_secrets: StoredSecrets,
    old_key: bytes,
    new_key: bytes,
) -> None:
    before = await every_secret(owner_engine)

    report = await rewrap_data_keys(owner_engine, provider_with(new_key, old_key))

    after = await every_secret(owner_engine)
    assert report == RewrapReport(
        active_key_id=local_key_id(new_key), rewrapped=SECRET_COUNT, remaining=0
    )
    assert {row.key_id for row in before} == {local_key_id(old_key)}
    assert {row.key_id for row in after} == {local_key_id(new_key)}
    assert sealed_values(after) == sealed_values(before)
    assert all(new.wrapped_dek != old.wrapped_dek for new, old in zip(after, before, strict=True))
    await assert_every_secret_decrypts(app_engine, provider_with(new_key), stored_secrets)


async def test_a_second_rotation_rewraps_nothing(
    owner_engine: AsyncEngine, stored_secrets: StoredSecrets, old_key: bytes, new_key: bytes
) -> None:
    provider = provider_with(new_key, old_key)
    await rewrap_data_keys(owner_engine, provider)
    before = await every_secret(owner_engine)

    report = await rewrap_data_keys(owner_engine, provider)

    assert report == RewrapReport(active_key_id=local_key_id(new_key), rewrapped=0, remaining=0)
    assert await every_secret(owner_engine) == before


async def test_an_interrupted_rotation_keeps_finished_batches_and_a_rerun_completes_it(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    stored_secrets: StoredSecrets,
    old_key: bytes,
    new_key: bytes,
) -> None:
    provider = provider_with(new_key, old_key)

    with pytest.raises(InjectedWrapFailure):
        await rewrap_data_keys(
            owner_engine, WrapFailsAfter(provider, successful_wraps=2), batch_size=2
        )

    key_ids = [row.key_id for row in await every_secret(owner_engine)]
    assert sorted(key_ids) == sorted([local_key_id(new_key)] * 2 + [local_key_id(old_key)] * 3)

    report = await rewrap_data_keys(owner_engine, provider, batch_size=2)

    assert report == RewrapReport(active_key_id=local_key_id(new_key), rewrapped=3, remaining=0)
    await assert_every_secret_decrypts(app_engine, provider_with(new_key), stored_secrets)


async def test_rotation_names_a_secret_whose_key_is_not_in_the_keyring(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    stored_secrets: StoredSecrets,
    old_key: bytes,
    new_key: bytes,
) -> None:
    lost_key = new_master_key()
    workspace_id = next(iter(stored_secrets.workspace_ids.values()))
    async with workspace_transaction(app_engine, workspace_id) as connection:
        orphan_id = await SecretService(provider_with(lost_key)).put(
            connection, workspace_id=workspace_id, plaintext=SecretStr("sk-live-orphaned-value")
        )
    before = await every_secret(owner_engine)

    with pytest.raises(DataKeyRewrapError) as raised:
        await rewrap_data_keys(owner_engine, provider_with(new_key, old_key))

    error = raised.value
    assert (error.secret_id, error.workspace_id, error.key_id) == (
        orphan_id,
        workspace_id,
        local_key_id(lost_key),
    )
    for named in (str(orphan_id), str(workspace_id), local_key_id(lost_key)):
        assert named in str(error)
    # The one batch rolled back, so nothing changed.
    assert await every_secret(owner_engine) == before


async def test_rotation_waits_for_a_concurrent_replacement_and_rewraps_its_new_data_key(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    only_these_tenants: TwoTenants,
    old_key: bytes,
    new_key: bytes,
) -> None:
    """An API instance still on the old keyring replaces a secret while rotation runs. Rotation
    must re-wrap the replacement's data key, not write the old one back over it."""
    workspace_id = only_these_tenants.first.workspace_id
    api = SecretService(provider_with(old_key))
    async with workspace_transaction(app_engine, workspace_id) as connection:
        secret_id = await api.put(
            connection, workspace_id=workspace_id, plaintext=SecretStr("sk-live-before-rotation")
        )

    async with workspace_transaction(app_engine, workspace_id) as connection:
        await api.rotate(
            connection,
            workspace_id=workspace_id,
            secret_id=secret_id,
            new_plaintext=SecretStr("sk-live-replaced-meanwhile"),
        )
        rotation = asyncio.create_task(
            rewrap_data_keys(owner_engine, provider_with(new_key, old_key))
        )
        rotation_waited = await lock_wait_seen_within(superuser_engine, LOCK_WAIT_TIMEOUT_SECONDS)
    report = await rotation

    assert rotation_waited, "rotation never waited on the replacement's row lock"
    assert report == RewrapReport(active_key_id=local_key_id(new_key), rewrapped=1, remaining=0)
    assert [row.key_id for row in await every_secret(owner_engine)] == [local_key_id(new_key)]
    async with workspace_transaction(app_engine, workspace_id) as connection:
        value = await SecretService(provider_with(new_key)).get(
            connection, workspace_id=workspace_id, secret_id=secret_id
        )
    assert value.get_secret_value() == "sk-live-replaced-meanwhile"


async def test_rotation_stops_when_the_provider_wraps_under_a_key_other_than_its_active_one(
    owner_engine: AsyncEngine, stored_secrets: StoredSecrets, old_key: bytes, new_key: bytes
) -> None:
    provider = WrapsUnderAnotherKey(provider_with(new_key, old_key))

    async with asyncio.timeout(ROTATION_TIMEOUT_SECONDS):
        with pytest.raises(KeyProviderContractError) as raised:
            await rewrap_data_keys(owner_engine, provider)

    assert (raised.value.active_key_id, raised.value.wrapped_key_id) == (
        CLAIMED_KEY_ID,
        local_key_id(new_key),
    )
    assert "broke its contract" in str(raised.value)
    assert {row.key_id for row in await every_secret(owner_engine)} == {local_key_id(old_key)}


async def test_the_owner_reads_every_workspaces_secrets_but_cannot_insert_or_delete_them(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    workspaces = {
        "first": two_tenants.first.workspace_id,
        "second": two_tenants.second.workspace_id,
    }

    async with owner_engine.connect() as connection:
        visible = (await connection.execute(SECRETS_OF_WORKSPACES, workspaces)).scalars().all()
        connectors = (await connection.execute(COUNT_CONNECTORS)).scalar_one()
    async with owner_engine.begin() as connection:
        deleted = await connection.execute(DELETE_SECRETS, workspaces)
    with pytest.raises(
        DBAPIError, match='new row violates row-level security policy for table "secrets"'
    ):
        async with owner_engine.begin() as connection:
            await connection.execute(
                INSERT_SECRET,
                {
                    "workspace_id": workspaces["first"],
                    "sealed": b"sealed",
                    "nonce": bytes(SECRET_NONCE_BYTES),
                },
            )
    async with app_engine.connect() as connection:
        visible_to_app_outside_a_workspace = (await connection.execute(COUNT_SECRETS)).scalar_one()

    assert set(visible) == set(workspaces.values())
    assert deleted.rowcount == 0
    assert connectors == 0
    assert visible_to_app_outside_a_workspace == 0


async def test_flowbuilder_keys_rotate_rewraps_every_data_key(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    stored_secrets: StoredSecrets,
    old_key: bytes,
    new_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MASTER_KEYS", encoded(new_key, old_key))
    monkeypatch.delenv("MASTER_KEYS_FILE", raising=False)

    # main runs its own event loop, so it cannot run on this test's.
    exit_code = await asyncio.to_thread(main, ["keys", "rotate"])

    output = capsys.readouterr()
    assert exit_code == 0
    assert output.out == (
        f"rewrapped {SECRET_COUNT} data keys to {local_key_id(new_key)};"
        " 0 remain under other keys\n"
    )
    assert output.err == ""
    assert {row.key_id for row in await every_secret(owner_engine)} == {local_key_id(new_key)}
    await assert_every_secret_decrypts(app_engine, provider_with(new_key), stored_secrets)
