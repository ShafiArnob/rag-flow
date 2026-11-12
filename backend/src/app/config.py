import re
from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MINIMUM_PASSWORD_LENGTH = 16
# Compose interpolates role passwords into database URLs unencoded, so they must need no encoding.
URL_SAFE_PASSWORD = re.compile(rf"[A-Za-z0-9._~-]{{{MINIMUM_PASSWORD_LENGTH},}}")


class Settings(BaseSettings):
    """Runtime configuration read from environment variables, documented in .env.example."""

    model_config = SettingsConfigDict(extra="ignore")

    # Connects as the application role, which row-level security applies to.
    database_url: SecretStr
    redis_url: str
    object_storage_health_url: str
    otel_exporter_otlp_endpoint: str
    # No default: a forgotten value must stop the service, not quietly send the cookie over HTTP.
    session_cookie_secure: bool
    otel_service_name: str = "flowbuilder-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class MigrationSettings(BaseSettings):
    """Configuration for Alembic, which connects as the role that owns the schema."""

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    migration_database_url: SecretStr


class KeyringSettings(BaseSettings):
    """Where the master keys that wrap secrets' data keys come from, documented in .env.example.

    Separate from Settings so the key rotation command needs only this and the owner's URL.
    """

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    master_keys: SecretStr | None = None
    master_keys_file: Path | None = None

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> Self:
        if (self.master_keys is None) == (self.master_keys_file is None):
            raise ValueError("set exactly one of MASTER_KEYS and MASTER_KEYS_FILE")
        return self


class BootstrapSettings(BaseSettings):
    """Configuration for app.database.bootstrap, which connects as the superuser."""

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    bootstrap_database_url: SecretStr
    database_owner_password: SecretStr
    database_app_password: SecretStr

    @field_validator("database_owner_password", "database_app_password")
    @classmethod
    def require_url_safe_password(cls, password: SecretStr) -> SecretStr:
        if URL_SAFE_PASSWORD.fullmatch(password.get_secret_value()) is None:
            raise ValueError(
                f"must be at least {MINIMUM_PASSWORD_LENGTH} characters,"
                " using only A-Z, a-z, 0-9 and . _ ~ -"
            )
        return password
