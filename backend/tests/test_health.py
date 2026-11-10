import io
import json
import logging
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.health import ReadinessCheck
from app.logging_config import JsonFormatter, RedactionFilter
from app.main import create_app


async def healthy() -> None:
    return None


async def broken() -> None:
    raise ConnectionError("postgres down, password=hunter2")


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("app")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    yield stream
    logger.handlers = []
    logger.propagate = True


async def client_for(
    span_exporter: InMemorySpanExporter, checks: dict[str, ReadinessCheck]
) -> AsyncIterator[httpx.AsyncClient]:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    app = create_app(provider, checks)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def client(span_exporter: InMemorySpanExporter) -> AsyncIterator[httpx.AsyncClient]:
    async for client in client_for(span_exporter, {"postgres": healthy, "redis": healthy}):
        yield client


@pytest.fixture
async def broken_client(span_exporter: InMemorySpanExporter) -> AsyncIterator[httpx.AsyncClient]:
    async for client in client_for(span_exporter, {"postgres": broken, "redis": healthy}):
        yield client


async def test_healthz_reports_alive(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_healthz_produces_a_trace(
    client: httpx.AsyncClient, span_exporter: InMemorySpanExporter
) -> None:
    await client.get("/healthz")

    server_spans = [span for span in span_exporter.get_finished_spans() if "/healthz" in span.name]
    assert server_spans


async def test_healthz_produces_a_redacted_json_log_line_tied_to_the_trace(
    client: httpx.AsyncClient, span_exporter: InMemorySpanExporter, log_stream: io.StringIO
) -> None:
    await client.get("/healthz", headers={"Authorization": "Bearer abc123xyz"})

    line = json.loads(log_stream.getvalue().strip().splitlines()[-1])
    trace_ids = {
        format(span.context.trace_id, "032x") for span in span_exporter.get_finished_spans()
    }
    assert line["path"] == "/healthz"
    assert line["status_code"] == 200
    assert line["trace_id"] in trace_ids
    assert "abc123xyz" not in log_stream.getvalue()


async def test_readyz_reports_ready_when_every_dependency_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"postgres": "ok", "redis": "ok"}


async def test_readyz_returns_503_and_names_the_failing_dependency(
    broken_client: httpx.AsyncClient, log_stream: io.StringIO
) -> None:
    response = await broken_client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"postgres": "unavailable", "redis": "ok"}
    assert "hunter2" not in log_stream.getvalue()
