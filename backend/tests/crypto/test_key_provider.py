"""The local key provider and the keyring it is loaded from. No error or repr may contain a key."""

import base64
import hashlib
import hmac
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from app.config import KeyringSettings
from app.crypto.envelope import DATA_KEY_BYTES, new_data_key
from app.crypto.key_provider import (
    MASTER_KEY_BYTES,
    KeyringError,
    KeyUnwrapError,
    LocalKeyProvider,
    UnknownKeyIdError,
    load_local_key_provider,
    local_key_id,
    parse_master_keys,
)
from support.master_keys import (
    ALL_ZERO_PLACEHOLDER,
    encoded,
    in_typographic_quotes,
    new_master_key,
    provider_with,
)

LOCAL_KEY_ID = re.compile(r"local:[0-9a-f]{16}")
# What a data key is wrapped for: in the service, the secret's workspace and id.
CONTEXT = b"workspace-and-secret"
# 32 printable bytes with 16 distinct values: it passes the distinct-bytes rule, so only the
# printable rule refuses it.
PASSPHRASE_KEY = base64.b64encode(b"correct horse battery staple 123").decode()
QUOTED_KEY = in_typographic_quotes(encoded(new_master_key()))
# A key with exactly this many distinct byte values is the weakest one accepted.
MINIMUM_DISTINCT_BYTES = 16


def key_with_distinct_bytes(count: int) -> bytes:
    return bytes(index % count for index in range(MASTER_KEY_BYTES))


@pytest.fixture
def no_keyring_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """KeyringSettings also reads the environment, which must not add a second source."""
    monkeypatch.delenv("MASTER_KEYS", raising=False)
    monkeypatch.delenv("MASTER_KEYS_FILE", raising=False)


def keyring_error(text: str, source: str = "MASTER_KEYS") -> KeyringError:
    with pytest.raises(KeyringError) as raised:
        parse_master_keys(text, source)
    return raised.value


@given(data_key=st.binary(min_size=DATA_KEY_BYTES, max_size=DATA_KEY_BYTES), context=st.binary())
async def test_unwrap_returns_the_data_key_that_was_wrapped_for_any_context(
    data_key: bytes, context: bytes
) -> None:
    provider = provider_with(new_master_key())

    wrapped = await provider.wrap(data_key, context)

    assert wrapped.key_id == provider.active_key_id
    assert data_key not in wrapped.wrapped
    assert await provider.unwrap(wrapped.wrapped, wrapped.key_id, context) == data_key


@pytest.mark.parametrize(
    "other_context",
    [
        pytest.param(b"another-workspace-and-secret", id="other"),
        pytest.param(b"", id="empty"),
        pytest.param(CONTEXT[:-1], id="prefix"),
        pytest.param(CONTEXT + b"x", id="extended"),
    ],
)
async def test_unwrap_rejects_a_data_key_wrapped_for_another_context(other_context: bytes) -> None:
    provider = provider_with(new_master_key())
    wrapped = await provider.wrap(new_data_key(), CONTEXT)

    with pytest.raises(KeyUnwrapError, match=re.escape(wrapped.key_id)):
        await provider.unwrap(wrapped.wrapped, wrapped.key_id, other_context)


async def test_a_keyring_wraps_under_its_first_key_and_unwraps_under_every_key() -> None:
    old_key, new_key = new_master_key(), new_master_key()
    data_key = new_data_key()
    wrapped_under_old = await provider_with(old_key).wrap(data_key, CONTEXT)

    rotated = provider_with(new_key, old_key)
    wrapped_under_new = await rotated.wrap(data_key, CONTEXT)

    assert rotated.active_key_id == local_key_id(new_key)
    assert wrapped_under_new.key_id == local_key_id(new_key)
    for wrapped in (wrapped_under_old, wrapped_under_new):
        assert await rotated.unwrap(wrapped.wrapped, wrapped.key_id, CONTEXT) == data_key


def test_a_key_id_is_local_and_the_first_sixteen_hex_digits_of_an_hmac_under_the_key() -> None:
    key = new_master_key()

    key_id = local_key_id(key)

    assert LOCAL_KEY_ID.fullmatch(key_id)
    expected = hmac.new(key, b"flowbuilder/key-id", hashlib.sha256).hexdigest()[:16]
    assert key_id == "local:" + expected
    assert key_id != "local:" + hashlib.sha256(key).hexdigest()[:16]
    assert local_key_id(new_master_key()) != key_id


async def test_unwrap_names_an_unknown_key_id() -> None:
    wrapped = await provider_with(new_master_key()).wrap(new_data_key(), CONTEXT)
    other = provider_with(new_master_key())

    with pytest.raises(UnknownKeyIdError, match=re.escape(wrapped.key_id)):
        await other.unwrap(wrapped.wrapped, wrapped.key_id, CONTEXT)


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda blob: bytes([blob[0] ^ 1]) + blob[1:], id="nonce"),
        pytest.param(lambda blob: blob[:-1] + bytes([blob[-1] ^ 1]), id="tag"),
        pytest.param(lambda blob: blob[:-1], id="truncated"),
        pytest.param(lambda blob: b"", id="empty"),
    ],
)
async def test_unwrap_rejects_a_tampered_wrapped_key(tamper: Callable[[bytes], bytes]) -> None:
    provider = provider_with(new_master_key())
    wrapped = await provider.wrap(new_data_key(), CONTEXT)

    with pytest.raises(KeyUnwrapError, match=re.escape(wrapped.key_id)):
        await provider.unwrap(tamper(wrapped.wrapped), wrapped.key_id, CONTEXT)


async def test_unwrap_rejects_a_wrapped_key_relabelled_with_another_key_id() -> None:
    first_key, second_key = new_master_key(), new_master_key()
    provider = provider_with(first_key, second_key)
    wrapped = await provider.wrap(new_data_key(), CONTEXT)

    with pytest.raises(KeyUnwrapError):
        await provider.unwrap(wrapped.wrapped, local_key_id(second_key), CONTEXT)


def test_parse_returns_the_keys_in_order_separated_by_any_whitespace() -> None:
    first, second, third = new_master_key(), new_master_key(), new_master_key()
    text = f"  {encoded(first)}\t{encoded(second)}\n\n{encoded(third)}\n"

    assert parse_master_keys(text, "MASTER_KEYS") == [first, second, third]


def test_a_key_with_sixteen_distinct_byte_values_is_accepted() -> None:
    key = key_with_distinct_bytes(MINIMUM_DISTINCT_BYTES)

    assert parse_master_keys(encoded(key), "MASTER_KEYS") == [key]


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        pytest.param(encoded(new_master_key()[:31]), "decodes to 31 bytes", id="31-bytes"),
        pytest.param(encoded(new_master_key() + b"x"), "decodes to 33 bytes", id="33-bytes"),
        pytest.param("not base64!", "is not valid base64", id="not-base64"),
        pytest.param(encoded(new_master_key())[:-1], "is not valid base64", id="bad-padding"),
        pytest.param(ALL_ZERO_PLACEHOLDER, "distinct byte values", id="all-zero-placeholder"),
        pytest.param(PASSPHRASE_KEY, "printable text", id="passphrase"),
        pytest.param(QUOTED_KEY, "is not valid base64", id="smart-quotes"),
        pytest.param(
            encoded(key_with_distinct_bytes(MINIMUM_DISTINCT_BYTES - 1)),
            "distinct byte values",
            id="15-distinct-bytes",
        ),
    ],
)
def test_parse_rejects_a_weak_or_malformed_key_without_echoing_it(value: str, reason: str) -> None:
    good_key = encoded(new_master_key())

    error = keyring_error(f"{good_key}\n{value}", "MASTER_KEYS_FILE /run/keys")

    assert str(error).startswith("MASTER_KEYS_FILE /run/keys: key 2, on line 2,")
    assert reason in str(error)
    for key_material in (value, good_key):
        assert key_material not in str(error)
        assert key_material not in repr(error)


@pytest.mark.parametrize("text", ["", "   \n\t\n"])
def test_parse_rejects_an_empty_keyring(text: str) -> None:
    assert str(keyring_error(text)) == "MASTER_KEYS holds no master key"


def test_parse_rejects_a_repeated_key_without_echoing_it() -> None:
    first, second = encoded(new_master_key()), encoded(new_master_key())

    error = keyring_error(f"{first} {second} {first}")

    assert str(error) == "MASTER_KEYS: key 3, on line 1, repeats key 1"


@pytest.mark.usefixtures("no_keyring_in_environment")
def test_load_reads_the_keyring_from_master_keys() -> None:
    new_key, old_key = new_master_key(), new_master_key()

    settings = KeyringSettings(master_keys=SecretStr(encoded(new_key, old_key)))

    provider = load_local_key_provider(settings)

    assert provider.active_key_id == local_key_id(new_key)


@pytest.mark.usefixtures("no_keyring_in_environment")
def test_load_reads_the_keyring_from_master_keys_file(tmp_path: Path) -> None:
    new_key, old_key = new_master_key(), new_master_key()
    keyring_file = tmp_path / "master-keys"
    keyring_file.write_text(f"{encoded(new_key)}\n{encoded(old_key)}\n")

    provider = load_local_key_provider(KeyringSettings(master_keys_file=keyring_file))

    assert provider.active_key_id == local_key_id(new_key)


@pytest.mark.usefixtures("no_keyring_in_environment")
def test_load_names_a_missing_master_keys_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(
        KeyringError, match=f"cannot read MASTER_KEYS_FILE {re.escape(str(missing))}"
    ):
        load_local_key_provider(KeyringSettings(master_keys_file=missing))


@pytest.mark.usefixtures("no_keyring_in_environment")
def test_load_rejects_a_master_keys_file_that_is_not_text_without_echoing_it(
    tmp_path: Path,
) -> None:
    keyring_file = tmp_path / "master-keys"
    keyring_file.write_bytes(b"\xff\xfe" + new_master_key())

    with pytest.raises(KeyringError, match="is not UTF-8 text") as raised:
        load_local_key_provider(KeyringSettings(master_keys_file=keyring_file))

    assert "\\x" not in str(raised.value)


@pytest.mark.usefixtures("no_keyring_in_environment")
def test_load_names_the_file_and_line_of_a_weak_key(tmp_path: Path) -> None:
    keyring_file = tmp_path / "master-keys"
    keyring_file.write_text(f"{encoded(new_master_key())}\n{ALL_ZERO_PLACEHOLDER}\n")

    with pytest.raises(KeyringError) as raised:
        load_local_key_provider(KeyringSettings(master_keys_file=keyring_file))

    assert str(raised.value).startswith(f"MASTER_KEYS_FILE {keyring_file}: key 2, on line 2,")
    assert ALL_ZERO_PLACEHOLDER not in str(raised.value)


@pytest.mark.usefixtures("no_keyring_in_environment")
def test_load_rejects_a_quoted_key_in_master_keys_without_echoing_it() -> None:
    settings = KeyringSettings(master_keys=SecretStr(QUOTED_KEY))

    with pytest.raises(KeyringError, match=r"^MASTER_KEYS: key 1, on line 1, is not valid base64"):
        load_local_key_provider(settings)


@pytest.mark.usefixtures("no_keyring_in_environment")
def test_load_rejects_a_quoted_key_in_master_keys_file_without_echoing_it(tmp_path: Path) -> None:
    keyring_file = tmp_path / "master-keys"
    keyring_file.write_text(f"{encoded(new_master_key())}\n{QUOTED_KEY}\n", encoding="utf-8")

    with pytest.raises(KeyringError) as raised:
        load_local_key_provider(KeyringSettings(master_keys_file=keyring_file))

    assert str(raised.value) == (
        f"MASTER_KEYS_FILE {keyring_file}: key 2, on line 2, is not valid base64"
    )


def test_a_provider_shows_only_key_ids() -> None:
    keys = [new_master_key(), new_master_key()]
    provider = LocalKeyProvider(keys)

    shown = f"{provider!r} {provider!s} {vars(provider)!r}"

    assert shown.count("local:") >= len(keys)
    for key in keys:
        for form in (key.hex(), base64.b64encode(key).decode(), repr(key)):
            assert form not in shown


def test_a_provider_needs_at_least_one_key() -> None:
    with pytest.raises(KeyringError, match="at least one master key"):
        LocalKeyProvider([])
