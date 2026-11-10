from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration read from environment variables, documented in .env.example."""

    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    redis_url: str
    object_storage_health_url: str
    otel_exporter_otlp_endpoint: str
    otel_service_name: str = "flowbuilder-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
