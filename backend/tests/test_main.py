"""The uvicorn factory. Its engine, Redis and HTTP clients connect only when first used, so building
the app needs no services."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from uvicorn.main import main as uvicorn_command

from app.main import create_app_from_environment
from support.settings_environment import DATABASE_PASSWORD, set_settings_environment

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"
EXEC_FORM_CMD_PREFIX = "CMD "


def image_command() -> list[str]:
    """The production image's command, from the exec-form CMD in backend/Dockerfile."""
    [instruction] = [
        line
        for line in DOCKERFILE.read_text().splitlines()
        if line.startswith(EXEC_FORM_CMD_PREFIX)
    ]
    command: list[str] = json.loads(instruction.removeprefix(EXEC_FORM_CMD_PREFIX))
    return command


class RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def startup_records(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[logging.LogRecord]]:
    """Collects app.main's records on a handler of its own: the factory's configure_logging
    replaces the root handlers, caplog's among them, and the root's settings are put back after."""
    set_settings_environment(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    root = logging.getLogger()
    root_handlers, root_level = root.handlers, root.level
    collector = RecordCollector()
    startup_logger = logging.getLogger("app.main")
    startup_logger.addHandler(collector)
    yield collector.records
    startup_logger.removeHandler(collector)
    root.handlers = root_handlers
    root.setLevel(root_level)


def test_startup_warns_once_without_secrets_when_the_session_cookie_is_not_secure(
    monkeypatch: pytest.MonkeyPatch, startup_records: list[logging.LogRecord]
) -> None:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    create_app_from_environment()

    [warning] = [record for record in startup_records if record.levelno == logging.WARNING]
    assert "SESSION_COOKIE_SECURE" in warning.getMessage()
    assert DATABASE_PASSWORD not in warning.getMessage()


def test_the_image_runs_uvicorn_with_proxy_headers_off() -> None:
    """With them on, uvicorn replaces the peer address with any X-Forwarded-For value sent from a
    loopback peer, so a client could choose the address the audit log records."""
    program, *arguments = image_command()
    assert program == "uvicorn"

    context = uvicorn_command.make_context(program, arguments)

    assert context.params["proxy_headers"] is False


def test_startup_does_not_warn_when_the_session_cookie_is_secure(
    monkeypatch: pytest.MonkeyPatch, startup_records: list[logging.LogRecord]
) -> None:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")

    create_app_from_environment()

    assert [record for record in startup_records if record.levelno >= logging.WARNING] == []
