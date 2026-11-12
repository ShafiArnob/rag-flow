import hashlib
import uuid

import pytest

from app.identity.sessions import (
    SessionCookie,
    decode_session_cookie,
    encode_session_cookie,
    hash_session_token,
    new_csrf_token,
    new_session_token,
)

WORKSPACE_ID = uuid.UUID("3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b")
TOKEN = new_session_token()


def test_cookie_value_round_trips() -> None:
    cookie = SessionCookie(workspace_id=WORKSPACE_ID, token=TOKEN)

    value = encode_session_cookie(cookie)

    assert value == f"{WORKSPACE_ID}.{TOKEN}"
    assert decode_session_cookie(value) == cookie


def test_every_new_token_decodes() -> None:
    for _ in range(200):
        token = new_session_token()
        cookie = SessionCookie(workspace_id=uuid.uuid4(), token=token)
        assert decode_session_cookie(encode_session_cookie(cookie)) == cookie


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        TOKEN,
        str(WORKSPACE_ID),
        f"{WORKSPACE_ID}.",
        f".{TOKEN}",
        f"not-a-uuid.{TOKEN}",
        f"{str(WORKSPACE_ID).upper()}.{TOKEN}",
        f"{WORKSPACE_ID.hex}.{TOKEN}",
        f"{{{WORKSPACE_ID}}}.{TOKEN}",
        f"{WORKSPACE_ID}.{TOKEN[:-1]}",
        f"{WORKSPACE_ID}.{TOKEN}a",
        f"{WORKSPACE_ID}.{TOKEN[:-1]}=",
        f"{WORKSPACE_ID}.{TOKEN[:-1]} ",
        f"{WORKSPACE_ID}.{TOKEN}.{TOKEN}",
        f" {WORKSPACE_ID}.{TOKEN}",
        f"{WORKSPACE_ID}.{TOKEN}\n",
        f"{WORKSPACE_ID}:{TOKEN}",
    ],
)
def test_malformed_cookie_value_decodes_to_nothing(value: str) -> None:
    assert decode_session_cookie(value) is None


def test_token_hash_is_the_sha256_of_the_token() -> None:
    assert hash_session_token(TOKEN) == hashlib.sha256(TOKEN.encode()).digest()
    assert len(hash_session_token(TOKEN)) == 32


def test_new_tokens_are_distinct() -> None:
    assert new_session_token() != new_session_token()
    assert new_csrf_token() != new_csrf_token()
