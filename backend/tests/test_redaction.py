import pytest

from app.redaction import REDACTED, redact_text, redact_value


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Authorization: Bearer abc.def-123", "abc.def-123"),
        ("password=hunter2 user=bob", "hunter2"),
        ("secret: s3cr3t-value", "s3cr3t-value"),
        ("calling with api_key=AKIA1234567890", "AKIA1234567890"),
        ("key sk-abcdefghijklmnop1234 leaked", "sk-abcdefghijklmnop1234"),
    ],
)
def test_redact_text_removes_secret(raw: str, secret: str) -> None:
    assert secret not in redact_text(raw)


def test_redact_text_keeps_surrounding_context() -> None:
    assert redact_text("login failed password=hunter2 for bob") == (
        f"login failed password={REDACTED} for bob"
    )


def test_redact_text_leaves_harmless_text_unchanged() -> None:
    assert redact_text("workflow saved in 12ms") == "workflow saved in 12ms"


@pytest.mark.parametrize(
    "key",
    ["password", "Secret", "access_token", "Authorization", "api_key", "key", "client_secret"],
)
def test_redact_value_masks_sensitive_keys(key: str) -> None:
    assert redact_value({key: "value", "name": "demo"}) == {key: REDACTED, "name": "demo"}


def test_redact_value_walks_nested_containers() -> None:
    value = {"headers": [{"password": "p"}, "Bearer tok123"], "count": 3}

    assert redact_value(value) == {
        "headers": [{"password": REDACTED}, f"Bearer {REDACTED}"],
        "count": 3,
    }


def test_redact_value_does_not_mutate_input() -> None:
    original = {"password": "p"}

    redact_value(original)

    assert original == {"password": "p"}
