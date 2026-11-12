"""Fake values for every setting Settings requires except SESSION_COOKIE_SECURE, for tests that
build Settings from the environment without any services."""

import pytest

# The database password in DATABASE_URL.
DATABASE_PASSWORD = "app-pw"  # noqa: S105 - a fake value that reaches no database
SETTINGS_ENVIRONMENT = {
    "DATABASE_URL": f"postgresql+asyncpg://flowbuilder_app:{DATABASE_PASSWORD}@db/example_test",
    "REDIS_URL": "redis://redis:6379/0",
    "OBJECT_STORAGE_HEALTH_URL": "http://minio:9000/minio/health/ready",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318",
}


def set_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable, value in SETTINGS_ENVIRONMENT.items():
        monkeypatch.setenv(variable, value)
