"""The egress client's behaviour on allowed hosts: pinning, redirects, headers, credentials and
limits."""

import traceback

import httpcore
import pytest
from pydantic import SecretStr

from app.egress.client import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_REDIRECTS,
    UNREACHABLE_HOST_REASON,
    EgressClient,
    EgressConnectionError,
    EgressCredential,
    EgressRefusedError,
    EgressResponseTooLargeError,
    EgressTimeoutError,
)
from support.fake_network import STALL, FakeNetwork, FakeResolver, http_response, redirect

PUBLIC_ADDRESS = "93.184.215.14"
OTHER_PUBLIC_ADDRESS = "1.1.1.1"
TOKEN = "s3cret-token-value"  # noqa: S105 - a fake value that reaches no service
CREDENTIAL = EgressCredential(header_name="Authorization", value=SecretStr(f"Bearer {TOKEN}"))


def resolver() -> FakeResolver:
    return FakeResolver(
        {
            "api.example.test": [[PUBLIC_ADDRESS]],
            "other.example.test": [[OTHER_PUBLIC_ADDRESS]],
            "internal.example.test": [["10.0.0.5"]],
        }
    )


def client_on(
    network: FakeNetwork,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> EgressClient:
    return EgressClient(
        resolver=resolver(),
        network_backend=network,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )


async def test_a_get_returns_the_response_from_the_checked_address() -> None:
    network = FakeNetwork([http_response(200, headers={"X-Kind": "demo"}, body=b"hello")])

    response = await client_on(network).request("GET", "https://api.example.test/v1/items?page=2")

    assert response.status_code == 200
    assert response.body == b"hello"
    assert ("X-Kind", "demo") in response.headers
    assert response.final_url == "https://api.example.test/v1/items?page=2"
    [connection] = network.connections
    assert (connection.host, connection.port) == (PUBLIC_ADDRESS, 443)
    assert connection.tls_server_name == "api.example.test"
    assert connection.written.startswith(b"GET /v1/items?page=2 HTTP/1.1\r\n")
    assert b"Host: api.example.test\r\n" in connection.written
    assert b"Accept-Encoding: identity\r\n" in connection.written


@pytest.mark.parametrize(
    "name",
    [
        "Host",
        "Accept-Encoding",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Upgrade",
        "Proxy-Authorization",
    ],
)
async def test_headers_the_client_sets_are_refused_from_the_caller(name: str) -> None:
    network = FakeNetwork([])

    with pytest.raises(EgressRefusedError, match=f"the {name} header is set by the egress client"):
        await client_on(network).request("GET", "https://api.example.test/", headers={name: "1"})

    assert network.connections == []


@pytest.mark.parametrize(
    ("headers", "credential"),
    [
        ({"X-Note": "line\r\nX-Injected: 1"}, None),
        ({"X-Note\n": "value"}, None),
        ({}, EgressCredential(header_name="Authorization", value=SecretStr(f"{TOKEN}\r\nX: 1"))),
    ],
)
async def test_a_header_with_a_line_break_is_refused_without_quoting_its_value(
    headers: dict[str, str], credential: EgressCredential | None
) -> None:
    network = FakeNetwork([])

    with pytest.raises(EgressRefusedError, match="line break or NUL") as refused:
        await client_on(network).request(
            "GET", "https://api.example.test/", headers=headers, credential=credential
        )

    rendered = "".join(traceback.format_exception(refused.value))
    assert TOKEN not in rendered
    assert "Injected" not in rendered
    assert network.connections == []


async def test_a_caller_header_that_duplicates_the_credential_is_refused() -> None:
    with pytest.raises(EgressRefusedError, match="also set by the credential"):
        await client_on(FakeNetwork([])).request(
            "GET",
            "https://api.example.test/",
            headers={"authorization": "Bearer other"},
            credential=CREDENTIAL,
        )


async def test_up_to_three_redirects_are_followed() -> None:
    network = FakeNetwork(
        [redirect(302, "/one"), redirect(301, "/two"), redirect(307, "/three"), http_response(200)]
    )

    response = await client_on(network).request("GET", "https://api.example.test/")

    assert response.status_code == 200
    assert response.final_url == "https://api.example.test/three"
    assert len(network.connections) == MAX_REDIRECTS + 1


async def test_a_fourth_redirect_is_refused_without_following_it() -> None:
    network = FakeNetwork([redirect(302, f"/hop-{index}") for index in range(MAX_REDIRECTS + 1)])

    with pytest.raises(EgressRefusedError, match="more than 3 redirects"):
        await client_on(network).request("GET", "https://api.example.test/")

    assert len(network.connections) == MAX_REDIRECTS + 1


async def test_each_redirect_target_is_resolved_and_checked() -> None:
    network = FakeNetwork([redirect(302, "https://other.example.test/next"), http_response(200)])
    lookups = resolver()
    client = EgressClient(resolver=lookups, network_backend=network)

    await client.request("GET", "https://api.example.test/")

    assert lookups.lookups == ["api.example.test", "other.example.test"]
    assert [connection.host for connection in network.connections] == [
        PUBLIC_ADDRESS,
        OTHER_PUBLIC_ADDRESS,
    ]


async def test_the_credential_and_headers_stay_behind_when_a_redirect_leaves_the_origin() -> None:
    network = FakeNetwork(
        [
            redirect(302, "https://other.example.test/next"),
            redirect(302, "https://api.example.test/back"),
            http_response(200),
        ]
    )

    await client_on(network).request(
        "GET", "https://api.example.test/", headers={"X-Api-Key": "key-123"}, credential=CREDENTIAL
    )

    sent = [
        (TOKEN.encode() in connection.written, b"key-123" in connection.written)
        for connection in network.connections
    ]
    assert sent == [(True, True), (False, False), (False, False)]


async def test_the_credential_and_headers_are_kept_on_a_same_origin_redirect() -> None:
    network = FakeNetwork([redirect(302, "/next"), http_response(200)])

    await client_on(network).request(
        "GET", "https://api.example.test/", headers={"X-Api-Key": "key-123"}, credential=CREDENTIAL
    )

    for connection in network.connections:
        assert TOKEN.encode() in connection.written
        assert b"key-123" in connection.written


@pytest.mark.parametrize(
    ("location", "allow_plain_http"),
    [("https://api.example.test:8443/next", False), ("http://api.example.test/next", True)],
)
async def test_the_credential_is_dropped_when_only_the_port_or_scheme_changes(
    location: str, allow_plain_http: bool
) -> None:
    network = FakeNetwork([redirect(302, location), http_response(200)])

    await client_on(network).request(
        "GET",
        "https://api.example.test/",
        credential=CREDENTIAL,
        allow_plain_http=allow_plain_http,
    )

    assert TOKEN.encode() not in network.connections[1].written


async def test_a_credential_is_never_sent_over_plain_http() -> None:
    network = FakeNetwork([])

    with pytest.raises(EgressRefusedError, match="only sent over https"):
        await client_on(network).request(
            "GET", "http://api.example.test/", credential=CREDENTIAL, allow_plain_http=True
        )

    assert network.connections == []


@pytest.mark.parametrize(
    ("status", "method", "followed_with"),
    [
        (303, "POST", b"GET"),
        (303, "HEAD", b"HEAD"),
        (302, "POST", b"GET"),
        (301, "POST", b"GET"),
        (302, "GET", b"GET"),
    ],
)
async def test_a_redirect_switches_the_method_as_browsers_do(
    status: int, method: str, followed_with: bytes
) -> None:
    network = FakeNetwork([redirect(status, "/result"), http_response(200)])
    body = b"payload" if method == "POST" else None

    await client_on(network).request(method, "https://api.example.test/jobs", body=body)

    second = network.connections[1].written
    assert second.startswith(followed_with + b" /result ")
    assert b"payload" not in second


async def test_a_307_keeps_the_method_and_body_on_the_same_origin() -> None:
    network = FakeNetwork([redirect(307, "/retry"), http_response(200)])

    await client_on(network).request("PUT", "https://api.example.test/item", body=b"payload")

    assert network.connections[1].written.startswith(b"PUT /retry ")
    assert network.connections[1].written.endswith(b"payload")


async def test_a_307_to_another_origin_with_a_body_is_refused() -> None:
    network = FakeNetwork([redirect(307, "https://other.example.test/item")])

    with pytest.raises(EgressRefusedError, match="would resend the request body"):
        await client_on(network).request("PUT", "https://api.example.test/item", body=b"payload")

    assert len(network.connections) == 1


async def test_plain_http_is_refused_by_default() -> None:
    network = FakeNetwork([])

    with pytest.raises(EgressRefusedError, match="'http' scheme is not allowed"):
        await client_on(network).request("GET", "http://api.example.test/")

    assert network.connections == []


async def test_plain_http_is_sent_without_tls_when_the_caller_allows_it() -> None:
    network = FakeNetwork([http_response(200)])

    await client_on(network).request("GET", "http://api.example.test/", allow_plain_http=True)

    [connection] = network.connections
    assert (connection.host, connection.port, connection.tls_server_name) == (
        PUBLIC_ADDRESS,
        80,
        None,
    )


async def test_a_redirect_from_https_to_http_is_refused_by_default() -> None:
    network = FakeNetwork([redirect(302, "http://api.example.test/")])

    with pytest.raises(EgressRefusedError, match="'http' scheme is not allowed"):
        await client_on(network).request("GET", "https://api.example.test/")

    assert len(network.connections) == 1


async def test_credentials_in_the_url_are_refused_and_never_echoed() -> None:
    with pytest.raises(EgressRefusedError, match="credentials in the URL") as refused:
        await client_on(FakeNetwork([])).request("GET", "https://admin:hunter2@api.example.test/")

    assert "hunter2" not in str(refused.value)


async def test_an_unparseable_url_is_refused() -> None:
    with pytest.raises(EgressRefusedError, match="not valid"):
        await client_on(FakeNetwork([])).request("GET", "https://api.example.test:not-a-port/")


@pytest.mark.parametrize(
    ("host", "detail"),
    [
        ("internal.example.test", "10.0.0.5 is a private address"),
        ("missing.example.test", "the name does not resolve"),
    ],
)
async def test_an_unreachable_host_gets_one_message_that_names_no_address(
    host: str, detail: str
) -> None:
    with pytest.raises(EgressRefusedError) as refused:
        await client_on(FakeNetwork([])).request("GET", f"https://{host}/")

    assert refused.value.reason == UNREACHABLE_HOST_REASON
    assert "10.0.0.5" not in str(refused.value)
    assert refused.value.detail == detail


@pytest.mark.parametrize(
    "failure", [httpcore.ConnectError("connection refused"), httpcore.ConnectTimeout("timed out")]
)
async def test_a_connection_failure_is_an_egress_error(failure: Exception) -> None:
    with pytest.raises(EgressConnectionError, match=type(failure).__name__) as failed:
        await client_on(FakeNetwork([failure])).request("GET", "https://api.example.test/")

    assert failed.value.__context__ is None


async def test_a_malformed_response_is_a_connection_error_named_by_type_only() -> None:
    network = FakeNetwork([b"not http at all\r\n\r\n"])

    with pytest.raises(EgressConnectionError, match="RemoteProtocolError") as failed:
        await client_on(network).request("GET", "https://api.example.test/")

    assert failed.value.__context__ is None


async def test_a_streamed_body_over_the_cap_is_cut_off() -> None:
    network = FakeNetwork([http_response(200, body=b"x" * 11, declare_length=False)])

    with pytest.raises(EgressResponseTooLargeError, match="exceeded 10 bytes"):
        await client_on(network, max_response_bytes=10).request("GET", "https://api.example.test/")


async def test_a_declared_body_over_the_cap_is_cut_off() -> None:
    network = FakeNetwork([http_response(200, body=b"x" * 11)])

    with pytest.raises(EgressResponseTooLargeError):
        await client_on(network, max_response_bytes=10).request("GET", "https://api.example.test/")


async def test_a_body_at_the_cap_is_returned() -> None:
    network = FakeNetwork([http_response(200, body=b"x" * 10, declare_length=False)])

    response = await client_on(network, max_response_bytes=10).request(
        "GET", "https://api.example.test/"
    )

    assert response.body == b"x" * 10


async def test_a_head_response_declaring_a_large_body_is_returned() -> None:
    network = FakeNetwork([http_response(200, headers={"Content-Length": "209715200"})])

    response = await client_on(network, max_response_bytes=10).request(
        "HEAD", "https://api.example.test/large.pdf"
    )

    assert response.status_code == 200
    assert response.body == b""


async def test_a_compressed_response_is_refused() -> None:
    network = FakeNetwork([http_response(200, headers={"Content-Encoding": "gzip"}, body=b"x")])

    with pytest.raises(EgressRefusedError, match="compressed"):
        await client_on(network).request("GET", "https://api.example.test/")


async def test_a_request_that_outlasts_the_deadline_times_out() -> None:
    with pytest.raises(EgressTimeoutError, match=r"longer than 0\.05 seconds"):
        await client_on(FakeNetwork([STALL]), timeout_seconds=0.05).request(
            "GET", "https://api.example.test/"
        )


async def test_a_timeout_names_the_origin_that_stalled_after_a_redirect() -> None:
    network = FakeNetwork([redirect(302, "https://other.example.test/slow"), STALL])

    with pytest.raises(EgressTimeoutError) as timed_out:
        await client_on(network, timeout_seconds=0.05).request("GET", "https://api.example.test/")

    assert timed_out.value.origin == "https://other.example.test"


async def test_errors_name_the_origin_but_not_the_path_query_or_credential() -> None:
    with pytest.raises(EgressTimeoutError) as timed_out:
        await client_on(FakeNetwork([STALL]), timeout_seconds=0.05).request(
            "GET", "https://api.example.test/private/path?api_key=abc123", credential=CREDENTIAL
        )

    message = str(timed_out.value)
    assert "https://api.example.test" in message
    for leaked in ("private", "abc123", TOKEN):
        assert leaked not in message
