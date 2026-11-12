"""Fake values for every setting Settings requires except SESSION_COOKIE_SECURE, plus a test
keyring, for tests that build the app's settings from the environment without any services."""

import pytest

# The database password in DATABASE_URL.
DATABASE_PASSWORD = "app-pw"  # noqa: S105 - a fake value that reaches no database
# 32 random bytes generated once, so runs are repeatable; it protects nothing.
TEST_MASTER_KEY = "f7seoZILJ8TUaqfoATxLZztc0E94VOsHzskm58BpEyA="
SETTINGS_ENVIRONMENT = {
    "DATABASE_URL": f"postgresql+asyncpg://flowbuilder_app:{DATABASE_PASSWORD}@db/example_test",
    "REDIS_URL": "redis://redis:6379/0",
    "OBJECT_STORAGE_HEALTH_URL": "http://minio:9000/minio/health/ready",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318",
    "MASTER_KEYS": TEST_MASTER_KEY,
}


def set_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable, value in SETTINGS_ENVIRONMENT.items():
        monkeypatch.setenv(variable, value)
    # KeyringSettings accepts only one source of keys.
    monkeypatch.delenv("MASTER_KEYS_FILE", raising=False)
