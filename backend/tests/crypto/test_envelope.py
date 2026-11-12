"""Sealing one secret under its own data key, bound to its workspace and id."""

import uuid

import pytest
from cryptography.exceptions import InvalidTag
from hypothesis import given
from hypothesis import strategies as st

from app.crypto.envelope import (
    DATA_KEY_BYTES,
    NONCE_BYTES,
    SealedSecret,
    last4,
    new_data_key,
    open_sealed,
    seal,
    secret_aad,
)
from support.master_keys import new_master_key, provider_with

uuids = st.uuids()


def flip_first_bit(value: bytes) -> bytes:
    return bytes([value[0] ^ 1]) + value[1:]


@given(plaintext=st.binary(), aad=st.binary())
def test_open_returns_what_was_sealed_for_any_bytes_and_aad(plaintext: bytes, aad: bytes) -> None:
    data_key = new_data_key()

    assert open_sealed(data_key, seal(data_key, plaintext, aad), aad) == plaintext


@given(
    plaintext=st.binary(),
    workspace_id=uuids,
    secret_id=uuids,
    other_workspace_id=uuids,
    other_secret_id=uuids,
)
def test_open_fails_for_another_workspace_or_secret(
    plaintext: bytes,
    workspace_id: uuid.UUID,
    secret_id: uuid.UUID,
    other_workspace_id: uuid.UUID,
    other_secret_id: uuid.UUID,
) -> None:
    data_key = new_data_key()
    sealed = seal(data_key, plaintext, secret_aad(workspace_id, secret_id))

    for other_aad in (
        secret_aad(other_workspace_id, secret_id),
        secret_aad(workspace_id, other_secret_id),
    ):
        if other_aad == secret_aad(workspace_id, secret_id):
            continue
        with pytest.raises(InvalidTag):
            open_sealed(data_key, sealed, other_aad)


@given(plaintext=st.text(), workspace_id=uuids, secret_id=uuids)
async def test_a_value_sealed_and_wrapped_for_a_secret_comes_back_unchanged(
    plaintext: str, workspace_id: uuid.UUID, secret_id: uuid.UUID
) -> None:
    """The whole path a secret takes, without the database."""
    provider = provider_with(new_master_key())
    aad = secret_aad(workspace_id, secret_id)
    data_key = new_data_key()

    sealed = seal(data_key, plaintext.encode(), aad)
    wrapped = await provider.wrap(data_key, aad)
    unwrapped = await provider.unwrap(wrapped.wrapped, wrapped.key_id, aad)

    assert open_sealed(unwrapped, sealed, aad).decode() == plaintext


def test_open_fails_on_a_tampered_ciphertext_or_nonce_or_another_data_key() -> None:
    data_key = new_data_key()
    aad = secret_aad(uuid.uuid4(), uuid.uuid4())
    sealed = seal(data_key, b"sk-live-0123456789", aad)

    for tampered in (
        SealedSecret(ciphertext=flip_first_bit(sealed.ciphertext), nonce=sealed.nonce),
        SealedSecret(ciphertext=sealed.ciphertext, nonce=flip_first_bit(sealed.nonce)),
    ):
        with pytest.raises(InvalidTag):
            open_sealed(data_key, tampered, aad)
    with pytest.raises(InvalidTag):
        open_sealed(new_data_key(), sealed, aad)


def test_each_seal_uses_a_fresh_nonce() -> None:
    data_key = new_data_key()
    aad = secret_aad(uuid.uuid4(), uuid.uuid4())

    first = seal(data_key, b"same value", aad)
    second = seal(data_key, b"same value", aad)

    assert len(first.nonce) == NONCE_BYTES
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


def test_data_keys_are_fresh_256_bit_keys() -> None:
    assert len(new_data_key()) == DATA_KEY_BYTES
    assert new_data_key() != new_data_key()


def test_secret_aad_is_the_workspace_id_then_the_secret_id() -> None:
    workspace_id, secret_id = uuid.uuid4(), uuid.uuid4()

    assert secret_aad(workspace_id, secret_id) == workspace_id.bytes + secret_id.bytes


@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [
        ("", ""),
        ("a" * 10 + "yz", "aayz"),
        ("abcdefghijk", ""),
        ("abcdefgh" + "wxyz", "wxyz"),
        # Characters, not bytes: the stored hint is at most four characters long.
        (
            "secret-key-\U0001f511\U0001f512\U0001f513\U0001f510",
            "\U0001f511\U0001f512\U0001f513\U0001f510",
        ),
        ("密钥密钥密钥密钥密钥密钥", "密钥密钥"),
        ("短い秘密", ""),
    ],
)
def test_last4_is_the_last_four_characters_of_values_of_at_least_twelve(
    plaintext: str, expected: str
) -> None:
    assert last4(plaintext) == expected
