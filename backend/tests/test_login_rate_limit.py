import uuid

import pytest

from app.identity.login_rate_limit import email_account_key, lockout_seconds, user_account_key


@pytest.mark.parametrize(
    ("lockout_count", "seconds"),
    [(1, 60), (2, 120), (3, 240), (4, 480), (5, 900), (6, 900), (100, 900)],
)
def test_lockouts_double_from_a_minute_up_to_15_minutes(lockout_count: int, seconds: int) -> None:
    assert lockout_seconds(lockout_count) == seconds


def test_an_unknown_emails_key_ignores_case_and_does_not_hold_the_email() -> None:
    email = "Someone.Unknown@Example.test"

    key = email_account_key(email)

    assert key == email_account_key(email.lower())
    assert key != email_account_key("someone.else@example.test")
    assert "someone" not in key.lower()
    assert "example" not in key.lower()


def test_a_users_key_is_its_id_and_never_collides_with_an_emails() -> None:
    user_id = uuid.uuid4()

    assert user_account_key(user_id) == f"user:{user_id}"
    assert email_account_key(f"{user_id}@example.test").startswith("email:")
