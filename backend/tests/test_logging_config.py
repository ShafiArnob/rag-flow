import io
import json
import logging
from collections.abc import Iterator

import pytest

from app.observability.logging_config import JsonFormatter, RedactionFilter
from app.observability.redaction import REDACTED


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("test.logging_config")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield stream
    logger.handlers = []


def read_line(stream: io.StringIO) -> dict[str, object]:
    line: dict[str, object] = json.loads(stream.getvalue().strip())
    return line


def test_emits_one_json_object_per_record(log_stream: io.StringIO) -> None:
    logging.getLogger("test.logging_config").info("saved %s", "workflow", extra={"duration_ms": 4})

    line = read_line(log_stream)

    assert line["message"] == "saved workflow"
    assert line["level"] == "INFO"
    assert line["logger"] == "test.logging_config"
    assert line["duration_ms"] == 4


def test_redacts_bearer_token_in_message(log_stream: io.StringIO) -> None:
    logging.getLogger("test.logging_config").info("sent Authorization: Bearer abc123xyz")

    assert "abc123xyz" not in log_stream.getvalue()


def test_redacts_secret_in_format_arguments(log_stream: io.StringIO) -> None:
    logging.getLogger("test.logging_config").info("login %s", "password=hunter2")

    assert "hunter2" not in log_stream.getvalue()


@pytest.mark.parametrize("key", ["password", "secret", "api_key", "authorization"])
def test_redacts_sensitive_extra_fields(log_stream: io.StringIO, key: str) -> None:
    logging.getLogger("test.logging_config").info("event", extra={key: "do-not-log"})

    line = read_line(log_stream)

    assert line[key] == REDACTED
    assert "do-not-log" not in log_stream.getvalue()


def test_redacts_secrets_inside_nested_extra_fields(log_stream: io.StringIO) -> None:
    logging.getLogger("test.logging_config").info(
        "event", extra={"request": {"headers": {"Authorization": "Bearer abc123xyz"}}}
    )

    assert "abc123xyz" not in log_stream.getvalue()


def test_redacts_secrets_in_exception_traceback(log_stream: io.StringIO) -> None:
    try:
        raise ValueError("connect failed password=hunter2")
    except ValueError:
        logging.getLogger("test.logging_config").exception("boom")

    line = read_line(log_stream)

    assert "hunter2" not in str(line["exception"])
    assert "ValueError" in str(line["exception"])
