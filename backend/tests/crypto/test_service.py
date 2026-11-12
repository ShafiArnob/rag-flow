"""The secret service against the real database, as the application role in workspace
transactions."""

import uuid
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.crypto.envelope import NONCE_BYTES
from app.crypto.key_provider import KeyUnwrapError, LocalKeyProvider, UnknownKeyIdError
from app.crypto.service import SecretDecryptionError, SecretNotFoundError, SecretService
from app.database.tenancy import workspace_transaction
from support.master_keys import new_master_key, provider_with
from support.tenants import TwoTenants

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

STORED_SECRET = text(
    "SELECT ciphertext, wrapped_dek, key_id, nonce, last4, updated_at FROM secrets WHERE id = :id"
)
# Gives secret :target the sealed value of secret :source, as an attacker with write access to the
# table could.
COPY_SEALED_VALUE = text(
    "UPDATE secrets SET (ciphertext, wrapped_dek, key_id, nonce) ="
    " (SELECT ciphertext, wrapped_dek, key_id, nonce FROM secrets WHERE id = :source)"
    " WHERE id = :target"
)

REPLACE_WRAPPED_DATA_KEY = text("UPDATE secrets SET wrapped_dek = :wrapped_dek WHERE id = :id")

PLAINTEXTS = {
    "ascii": "sk-live-0123456789abcdef",
    "unicode": "pässwörd-密钥-\U0001f511-value",
    "long": "x" * 10_000 + "tail",
    "short": "abc",
}


@pytest.fixture
def provider() -> LocalKeyProvider:
    return provider_with(new_master_key())


@pytest.fixture
def service(provider: LocalKeyProvider) -> SecretService:
    return SecretService(provider)


async def put(
    engine: AsyncEngine, service: SecretService, workspace_id: uuid.UUID, plaintext: str
) -> uuid.UUID:
    async with workspace_transaction(engine, workspace_id) as connection:
        return await service.put(
            connection, workspace_id=workspace_id, plaintext=SecretStr(plaintext)
        )


async def get(
    engine: AsyncEngine, service: SecretService, workspace_id: uuid.UUID, secret_id: uuid.UUID
) -> str:
    async with workspace_transaction(engine, workspace_id) as connection:
        value = await service.get(connection, workspace_id=workspace_id, secret_id=secret_id)
    return value.get_secret_value()


async def stored_secret(
    engine: AsyncEngine, workspace_id: uuid.UUID, secret_id: uuid.UUID
) -> Row[Any]:
    async with workspace_transaction(engine, workspace_id) as connection:
        return (await connection.execute(STORED_SECRET, {"id": secret_id})).one()


@pytest.mark.parametrize("plaintext", list(PLAINTEXTS.values()), ids=list(PLAINTEXTS))
async def test_get_returns_what_put_stored(
    app_engine: AsyncEngine, two_tenants: TwoTenants, service: SecretService, plaintext: str
) -> None:
    workspace_id = two_tenants.first.workspace_id

    secret_id = await put(app_engine, service, workspace_id, plaintext)

    assert await get(app_engine, service, workspace_id, secret_id) == plaintext


@pytest.mark.parametrize(
    ("plaintext", "expected_last4"),
    [
        (PLAINTEXTS["ascii"], "cdef"),
        (PLAINTEXTS["unicode"], "alue"),
        (PLAINTEXTS["short"], ""),
        ("eleven-char", ""),
        ("twelve-chars", "hars"),
    ],
)
async def test_the_stored_row_holds_no_plaintext(
    app_engine: AsyncEngine,
    two_tenants: TwoTenants,
    provider: LocalKeyProvider,
    service: SecretService,
    plaintext: str,
    expected_last4: str,
) -> None:
    workspace_id = two_tenants.first.workspace_id

    secret_id = await put(app_engine, service, workspace_id, plaintext)

    row = await stored_secret(app_engine, workspace_id, secret_id)
    for stored in (row.ciphertext, row.wrapped_dek, row.nonce):
        assert plaintext.encode() not in stored
    assert len(row.nonce) == NONCE_BYTES
    assert row.key_id == provider.active_key_id
    assert row.last4 == expected_last4


async def test_get_of_another_workspaces_secret_or_an_unknown_id_is_not_found(
    app_engine: AsyncEngine, two_tenants: TwoTenants, service: SecretService
) -> None:
    first, second = two_tenants.first.workspace_id, two_tenants.second.workspace_id
    secret_id = await put(app_engine, service, first, PLAINTEXTS["ascii"])

    for workspace_id, missing_id in ((second, secret_id), (first, uuid.uuid4())):
        with pytest.raises(SecretNotFoundError, match=str(missing_id)):
            await get(app_engine, service, workspace_id, missing_id)


async def test_a_sealed_value_copied_onto_another_secret_fails_to_decrypt(
    app_engine: AsyncEngine, two_tenants: TwoTenants, service: SecretService
) -> None:
    workspace_id = two_tenants.first.workspace_id
    source = await put(app_engine, service, workspace_id, PLAINTEXTS["ascii"])
    target = await put(app_engine, service, workspace_id, "another-secret-value")

    async with workspace_transaction(app_engine, workspace_id) as connection:
        await connection.execute(COPY_SEALED_VALUE, {"source": source, "target": target})

    with pytest.raises(SecretDecryptionError, match=str(target)) as raised:
        await get(app_engine, service, workspace_id, target)
    assert PLAINTEXTS["ascii"] not in str(raised.value)
    assert await get(app_engine, service, workspace_id, source) == PLAINTEXTS["ascii"]


async def test_a_tampered_wrapped_data_key_fails_to_decrypt_naming_only_the_secret(
    app_engine: AsyncEngine, two_tenants: TwoTenants, service: SecretService
) -> None:
    workspace_id = two_tenants.first.workspace_id
    secret_id = await put(app_engine, service, workspace_id, PLAINTEXTS["ascii"])
    wrapped_dek = (await stored_secret(app_engine, workspace_id, secret_id)).wrapped_dek
    async with workspace_transaction(app_engine, workspace_id) as connection:
        await connection.execute(
            REPLACE_WRAPPED_DATA_KEY,
            {"id": secret_id, "wrapped_dek": wrapped_dek[:-1] + bytes([wrapped_dek[-1] ^ 1])},
        )

    with pytest.raises(SecretDecryptionError, match=str(secret_id)) as raised:
        await get(app_engine, service, workspace_id, secret_id)
    assert isinstance(raised.value.__cause__, KeyUnwrapError)


async def test_get_names_the_key_id_when_no_key_of_the_keyring_wrapped_the_data_key(
    app_engine: AsyncEngine,
    two_tenants: TwoTenants,
    provider: LocalKeyProvider,
    service: SecretService,
) -> None:
    workspace_id = two_tenants.first.workspace_id
    secret_id = await put(app_engine, service, workspace_id, PLAINTEXTS["ascii"])
    other_service = SecretService(provider_with(new_master_key()))

    with pytest.raises(UnknownKeyIdError, match=provider.active_key_id):
        await get(app_engine, other_service, workspace_id, secret_id)


async def test_rotate_replaces_the_value_under_the_same_id(
    app_engine: AsyncEngine, two_tenants: TwoTenants, service: SecretService
) -> None:
    workspace_id = two_tenants.first.workspace_id
    secret_id = await put(app_engine, service, workspace_id, PLAINTEXTS["ascii"])
    before = await stored_secret(app_engine, workspace_id, secret_id)

    async with workspace_transaction(app_engine, workspace_id) as connection:
        await service.rotate(
            connection,
            workspace_id=workspace_id,
            secret_id=secret_id,
            new_plaintext=SecretStr("sk-live-rotated-value-9876"),
        )

    after = await stored_secret(app_engine, workspace_id, secret_id)
    assert await get(app_engine, service, workspace_id, secret_id) == "sk-live-rotated-value-9876"
    assert after.last4 == "9876"
    assert after.ciphertext != before.ciphertext
    assert after.wrapped_dek != before.wrapped_dek
    assert after.nonce != before.nonce
    assert after.updated_at > before.updated_at


async def test_rotate_of_another_workspaces_secret_is_not_found_and_changes_nothing(
    app_engine: AsyncEngine, two_tenants: TwoTenants, service: SecretService
) -> None:
    first, second = two_tenants.first.workspace_id, two_tenants.second.workspace_id
    secret_id = await put(app_engine, service, first, PLAINTEXTS["ascii"])

    with pytest.raises(SecretNotFoundError, match=str(secret_id)):
        async with workspace_transaction(app_engine, second) as connection:
            await service.rotate(
                connection,
                workspace_id=second,
                secret_id=secret_id,
                new_plaintext=SecretStr("sk-live-attacker-value"),
            )

    assert await get(app_engine, service, first, secret_id) == PLAINTEXTS["ascii"]
