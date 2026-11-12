import pytest
from pydantic import ValidationError

from app.config import BootstrapSettings, KeyringSettings, Settings
from support.master_keys import encoded, new_master_key
from support.settings_environment import set_settings_environment

SAFE_PASSWORD = "safe-Password_0123.~"  # noqa: S105 - a fake value for validation tests


def test_settings_require_session_cookie_secure(monkeypatch: pytest.MonkeyPatch) -> None:
    set_settings_environment(monkeypatch)
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)

    with pytest.raises(ValidationError) as raised:
        Settings()  # type: ignore[call-arg]

    assert [error["loc"] for error in raised.value.errors()] == [("session_cookie_secure",)]
    assert raised.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_settings_read_session_cookie_secure(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    set_settings_environment(monkeypatch)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", value)

    assert Settings().session_cookie_secure is expected  # type: ignore[call-arg]


def set_bootstrap_environment(
    monkeypatch: pytest.MonkeyPatch, *, owner_password: str, app_password: str
) -> None:
    monkeypatch.setenv(
        "BOOTSTRAP_DATABASE_URL", "postgresql+asyncpg://flowbuilder:superuser-pw@db/example_test"
    )
    monkeypatch.setenv("DATABASE_OWNER_PASSWORD", owner_password)
    monkeypatch.setenv("DATABASE_APP_PASSWORD", app_password)


def test_bootstrap_settings_accept_url_safe_passwords_and_hide_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_bootstrap_environment(monkeypatch, owner_password=SAFE_PASSWORD, app_password="a" * 16)

    settings = BootstrapSettings()  # type: ignore[call-arg]

    assert settings.database_owner_password.get_secret_value() == SAFE_PASSWORD
    assert SAFE_PASSWORD not in repr(settings)
    assert "superuser-pw" not in repr(settings)


@pytest.mark.parametrize(
    "unsafe_password",
    [
        "fifteen-chars-x",
        "has@sign-password-1234",
        "has space password 1234",
        "has'quote-password-1234",
        "has%percent-password-1234",
        "has:colon/slash-password",
    ],
)
@pytest.mark.parametrize("variable", ["DATABASE_OWNER_PASSWORD", "DATABASE_APP_PASSWORD"])
def test_bootstrap_settings_reject_unsafe_or_short_password_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, variable: str, unsafe_password: str
) -> None:
    set_bootstrap_environment(monkeypatch, owner_password=SAFE_PASSWORD, app_password=SAFE_PASSWORD)
    monkeypatch.setenv(variable, unsafe_password)

    with pytest.raises(ValidationError) as raised:
        BootstrapSettings()  # type: ignore[call-arg]

    assert raised.value.errors()[0]["loc"] == (variable.lower(),)
    assert unsafe_password not in str(raised.value)
    assert unsafe_password not in repr(raised.value)


def test_keyring_settings_require_a_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MASTER_KEYS", raising=False)
    monkeypatch.delenv("MASTER_KEYS_FILE", raising=False)

    with pytest.raises(
        ValidationError, match="set exactly one of MASTER_KEYS and MASTER_KEYS_FILE"
    ):
        KeyringSettings()


def test_keyring_settings_refuse_both_sources_without_echoing_the_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_keys = encoded(new_master_key())
    monkeypatch.setenv("MASTER_KEYS", master_keys)
    monkeypatch.setenv("MASTER_KEYS_FILE", "/run/secrets/master-keys")

    with pytest.raises(ValidationError, match="set exactly one of") as raised:
        KeyringSettings()

    assert master_keys not in str(raised.value)
    assert master_keys not in repr(raised.value)


def test_keyring_settings_hide_the_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    master_keys = encoded(new_master_key())
    monkeypatch.setenv("MASTER_KEYS", master_keys)
    monkeypatch.delenv("MASTER_KEYS_FILE", raising=False)

    settings = KeyringSettings()

    assert settings.master_keys is not None
    assert settings.master_keys.get_secret_value() == master_keys
    assert master_keys not in repr(settings)
