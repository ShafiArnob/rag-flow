import pytest
from pydantic import ValidationError

from app.config import BootstrapSettings

SAFE_PASSWORD = "safe-Password_0123.~"  # noqa: S105 - a fake value for validation tests


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
