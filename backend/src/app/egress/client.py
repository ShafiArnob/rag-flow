"""The one outbound HTTP client. Every request to a URL that a user, a connector or a node supplies
goes through it, so none can reach an internal address (SSRF).

Each hop resolves the host once, refuses the request if any resolved address is not public, and
connects only to the address it checked. Redirects are followed by hand so each target gets the same
check, and whatever the caller meant for one origin (headers, credential, body) never follows a
redirect to another. Error messages name the origin (scheme, host and port) and never the path,
query, headers or credential, which may hold secrets.
"""

import ipaddress
import socket
from asyncio import timeout
from collections.abc import Mapping
from dataclasses import dataclass

import httpcore
import httpx
from pydantic import SecretStr

from app.egress.address_policy import IPAddress, refusal_reason
from app.egress.pinned_network import PinnedNetworkBackend
from app.egress.resolver import Resolver, SystemResolver

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
DEFAULT_PORTS = {"https": 443, "http": 80}
# Headers only the client sets. Host must match the checked URL; an identity encoding keeps the size
# cap about the body itself; and the framing headers follow from the body, so a caller can neither
# desynchronise them (request smuggling against the remote server) nor break the connection.
CLIENT_SET_HEADERS = frozenset(
    {
        "host",
        "accept-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "upgrade",
    }
)
CLIENT_SET_HEADER_PREFIX = "proxy-"
# Characters that would end a header early and start another.
_HEADER_BREAKING_CHARACTERS = ("\r", "\n", "\0")
# What a caller is told for any host it may not reach, whether the name does not resolve or
# resolves to a refused address, so errors cannot be used to map internal DNS.
UNREACHABLE_HOST_REASON = "the host does not resolve to an allowed address"


class EgressError(Exception):
    """An outbound request that did not produce a response."""


class EgressRefusedError(EgressError):
    def __init__(self, origin: str, reason: str, *, detail: str | None = None) -> None:
        super().__init__(f"refused request to {origin}: {reason}")
        self.origin = origin
        self.reason = reason
        # For server-side logs only: it can name an internal address, so the message leaves it out.
        self.detail = detail


class EgressConnectionError(EgressError):
    def __init__(self, origin: str, reason: str) -> None:
        super().__init__(f"request to {origin} failed: {reason}")
        self.origin = origin
        self.reason = reason


class EgressResponseTooLargeError(EgressError):
    def __init__(self, origin: str, limit_bytes: int) -> None:
        super().__init__(f"response from {origin} exceeded {limit_bytes} bytes")
        self.origin = origin
        self.limit_bytes = limit_bytes


class EgressTimeoutError(EgressError):
    def __init__(self, origin: str, timeout_seconds: float) -> None:
        super().__init__(f"request to {origin} took longer than {timeout_seconds} seconds")
        self.origin = origin
        self.timeout_seconds = timeout_seconds


@dataclass(frozen=True)
class EgressCredential:
    """A header that authenticates to one origin, such as ``Authorization: Bearer <token>``. It is
    sent only over https, only to the origin of the request's URL, and dropped as soon as a redirect
    leaves it."""

    header_name: str
    value: SecretStr


@dataclass(frozen=True)
class EgressResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    final_url: str


@dataclass(frozen=True)
class _Hop:
    method: str
    url: httpx.URL
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    credential: EgressCredential | None


@dataclass(frozen=True)
class _Redirect:
    status_code: int
    location: str


def _origin(url: httpx.URL) -> str:
    # netloc never includes userinfo, so a password in the URL cannot reach a message.
    return f"{url.scheme}://{url.netloc.decode('ascii')}"


def _parse_url(url: str) -> httpx.URL:
    try:
        return httpx.URL(url)
    except httpx.InvalidURL:
        raise EgressRefusedError("<unparseable URL>", "the URL is not valid") from None


def _ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _header_refusal(name: str, value: str) -> str | None:
    """Why the client will not send this header, or None. Never quotes the value."""
    if any(character in name + value for character in _HEADER_BREAKING_CHARACTERS):
        return f"the {name!r} header contains a line break or NUL"
    lowered = name.lower()
    if lowered in CLIENT_SET_HEADERS or lowered.startswith(CLIENT_SET_HEADER_PREFIX):
        return f"the {name} header is set by the egress client"
    return None


def _validated_headers(
    origin: str, headers: Mapping[str, str], credential: EgressCredential | None
) -> tuple[tuple[str, str], ...]:
    pairs = list(headers.items())
    if credential is not None:
        if any(name.lower() == credential.header_name.lower() for name, _ in pairs):
            raise EgressRefusedError(
                origin, f"the {credential.header_name} header is also set by the credential"
            )
        pairs.append((credential.header_name, credential.value.get_secret_value()))
    for name, value in pairs:
        reason = _header_refusal(name, value)
        if reason is not None:
            raise EgressRefusedError(origin, reason)
    return tuple(headers.items())


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    return next((value for key, value in headers if key.lower() == name), None)


def _request_headers(hop: _Hop) -> list[tuple[bytes, bytes]]:
    request_headers = [(b"Host", hop.url.netloc), (b"Accept-Encoding", b"identity")]
    request_headers += [(name.encode(), value.encode()) for name, value in hop.headers]
    if hop.credential is not None:
        request_headers.append(
            (hop.credential.header_name.encode(), hop.credential.value.get_secret_value().encode())
        )
    return request_headers


def _next_hop(hop: _Hop, redirect: _Redirect) -> _Hop:
    origin = _origin(hop.url)
    try:
        target = hop.url.join(redirect.location)
    except httpx.InvalidURL:
        raise EgressRefusedError(origin, "it redirected to an invalid URL") from None
    is_same_origin = _origin(target) == origin
    method, body = hop.method, hop.body
    # As browsers do: 303 always switches to GET, and 301 and 302 do for anything but GET and HEAD.
    if (redirect.status_code == 303 and method != "HEAD") or (
        redirect.status_code in (301, 302) and method not in ("GET", "HEAD")
    ):
        method, body = "GET", None
    elif body is not None and not is_same_origin:
        raise EgressRefusedError(
            origin, "a redirect to another origin would resend the request body"
        )
    if is_same_origin:
        return _Hop(method, target, hop.headers, body, hop.credential)
    # What the caller set was meant for this origin, so it stays behind, for good: a later redirect
    # back to this origin does not restore it.
    return _Hop(method, target, headers=(), body=body, credential=None)


class EgressClient:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._resolver = SystemResolver() if resolver is None else resolver
        self._network_backend = (
            httpcore.AnyIOBackend() if network_backend is None else network_backend
        )
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._ssl_context = httpcore.default_ssl_context()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        credential: EgressCredential | None = None,
        allow_plain_http: bool = False,
    ) -> EgressResponse:
        """Sends the request and follows up to MAX_REDIRECTS redirects, all within one deadline.

        Raises EgressRefusedError before connecting anywhere the policy forbids, and the other
        EgressError subclasses when the request fails, the response is too large or time runs out.
        """
        parsed_url = _parse_url(url)
        hop = _Hop(
            method=method.upper(),
            url=parsed_url,
            headers=_validated_headers(_origin(parsed_url), headers or {}, credential),
            body=body,
            credential=credential,
        )
        try:
            async with timeout(self._timeout_seconds):
                for _ in range(MAX_REDIRECTS + 1):
                    address = await self._checked_address(hop, allow_plain_http=allow_plain_http)
                    outcome = await self._send(hop, address)
                    if isinstance(outcome, EgressResponse):
                        return outcome
                    hop = _next_hop(hop, outcome)
                raise EgressRefusedError(_origin(hop.url), f"more than {MAX_REDIRECTS} redirects")
        except TimeoutError:
            # hop is the one in flight, so the error names the origin that ran out the clock.
            raise EgressTimeoutError(_origin(hop.url), self._timeout_seconds) from None

    async def _checked_address(self, hop: _Hop, *, allow_plain_http: bool) -> IPAddress:
        """The address to connect to for the hop, once every address its host has is allowed."""
        url = hop.url
        origin = _origin(url)
        allowed_schemes = ("https", "http") if allow_plain_http else ("https",)
        if url.scheme not in allowed_schemes:
            raise EgressRefusedError(origin, f"the {url.scheme!r} scheme is not allowed")
        if hop.credential is not None and url.scheme != "https":
            raise EgressRefusedError(origin, "a credential is only sent over https")
        if url.userinfo:
            raise EgressRefusedError(origin, "credentials in the URL are not allowed")
        if not url.host:
            raise EgressRefusedError(origin, "the URL has no host")
        addresses = await self._resolve(url, origin)
        for address in addresses:
            reason = refusal_reason(address)
            if reason is not None:
                # Any refused address refuses the host: a mixed answer is how rebinding starts.
                raise EgressRefusedError(
                    origin, UNREACHABLE_HOST_REASON, detail=f"{address} is {reason}"
                )
        return addresses[0]

    async def _resolve(self, url: httpx.URL, origin: str) -> list[IPAddress]:
        literal = _ip_literal(url.host)
        if literal is not None:
            return [literal]
        port = url.port or DEFAULT_PORTS[url.scheme]
        try:
            addresses = await self._resolver(url.raw_host.decode("ascii"), port)
        except socket.gaierror:
            raise EgressRefusedError(
                origin, UNREACHABLE_HOST_REASON, detail="the name does not resolve"
            ) from None
        if not addresses:
            raise EgressRefusedError(
                origin, UNREACHABLE_HOST_REASON, detail="the name has no addresses"
            )
        return addresses

    async def _send(self, hop: _Hop, address: IPAddress) -> EgressResponse | _Redirect:
        origin = _origin(hop.url)
        try:
            return await self._exchange(hop, address, origin)
        except httpcore.ProtocolError as error:
            # Only the type: h11 can quote request data in its message.
            failure = type(error).__name__
        except (httpcore.NetworkError, httpcore.TimeoutException) as error:
            # Connect, read, write and TLS failures, and timeouts the OS reports. Their messages
            # come from the OS or the TLS library and hold no request data.
            failure = f"{type(error).__name__}: {error}"
        # Raised outside the except blocks, so the httpcore error is not kept as its context.
        raise EgressConnectionError(origin, failure)

    async def _exchange(
        self, hop: _Hop, address: IPAddress, origin: str
    ) -> EgressResponse | _Redirect:
        target = httpcore.URL(
            scheme=hop.url.raw_scheme,
            host=hop.url.raw_host,
            port=hop.url.port,
            target=hop.url.raw_path,
        )
        async with (
            httpcore.AsyncConnectionPool(
                ssl_context=self._ssl_context,
                network_backend=PinnedNetworkBackend(address, self._network_backend),
            ) as pool,
            pool.stream(
                hop.method, target, headers=_request_headers(hop), content=hop.body
            ) as response,
        ):
            location = _header(response.headers, b"location")
            if response.status in REDIRECT_STATUS_CODES and location is not None:
                return _Redirect(response.status, location.decode("latin-1"))
            encoding = _header(response.headers, b"content-encoding")
            if encoding is not None and encoding.strip().lower() != b"identity":
                # Decoding it could turn a body under the size cap into one far over it.
                raise EgressRefusedError(origin, "the response body is compressed")
            return EgressResponse(
                status_code=response.status,
                headers=tuple(
                    (key.decode("latin-1"), value.decode("latin-1"))
                    for key, value in response.headers
                ),
                body=await self._read_body(response, origin),
                final_url=str(hop.url),
            )

    async def _read_body(self, response: httpcore.Response, origin: str) -> bytes:
        # Counted as it arrives rather than trusted from Content-Length, which a HEAD or 304
        # response declares for a body it never sends.
        chunks: list[bytes] = []
        received = 0
        async for chunk in response.aiter_stream():
            received += len(chunk)
            if received > self._max_response_bytes:
                raise EgressResponseTooLargeError(origin, self._max_response_bytes)
            chunks.append(chunk)
        return b"".join(chunks)
