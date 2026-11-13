"""Stand-ins for DNS and sockets, the egress client's external boundaries.

FakeNetwork answers each TCP connection with the next canned HTTP response and records where the
connection went and what was written to it, so a test can see which address the client pinned and
which headers it sent. A connection the test did not expect fails the test.
"""

import asyncio
import ipaddress
import socket
import ssl
import typing
from dataclasses import dataclass, field

import httpcore

from app.egress.address_policy import IPAddress

# A response that never arrives, for deadline tests.
STALL = None


def http_response(
    status: int = 200,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    declare_length: bool = True,
) -> bytes:
    """Raw HTTP/1.1 response bytes. Without a declared length, the body runs to the end of the
    connection. A Content-Length in ``headers`` replaces the body's own."""
    headers = headers or {}
    lines = [f"HTTP/1.1 {status} Status"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    if "content-length" not in {name.lower() for name in headers}:
        lines.append(f"Content-Length: {len(body)}" if declare_length else "Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def redirect(status: int, location: str) -> bytes:
    return http_response(status, headers={"Location": location})


@dataclass
class Connection:
    host: str
    port: int
    written: bytearray = field(default_factory=bytearray)
    # The name TLS checked the certificate against; None when the connection stayed plain HTTP.
    tls_server_name: str | None = None


class _RecordingStream(httpcore.AsyncNetworkStream):
    def __init__(self, response: bytes | None, connection: Connection) -> None:
        self._unread = response
        self._connection = connection

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._unread is STALL:
            await asyncio.Event().wait()
        chunk, self._unread = (self._unread or b"")[:max_bytes], (self._unread or b"")[max_bytes:]
        return chunk

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._connection.written.extend(buffer)

    async def aclose(self) -> None:
        pass

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self._connection.tls_server_name = server_hostname
        return self

    def get_extra_info(self, info: str) -> typing.Any:
        return None


class FakeNetwork(httpcore.AsyncNetworkBackend):
    """Each entry answers one connection: response bytes, STALL, or an exception to raise instead
    of connecting."""

    def __init__(self, responses: list[bytes | Exception | None]) -> None:
        self._responses = list(responses)
        self.connections: list[Connection] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if not self._responses:
            raise AssertionError(f"unexpected connection to {host}:{port}")
        connection = Connection(host=host, port=port)
        self.connections.append(connection)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _RecordingStream(response, connection)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise AssertionError(f"unexpected Unix socket connection to {path}")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeResolver:
    """Answers each host from a list of answers, one per lookup; the last answer repeats. A host
    with no entry does not resolve."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self._answers = {host: list(sequence) for host, sequence in answers.items()}
        self.lookups: list[str] = []

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        self.lookups.append(host)
        sequence = self._answers.get(host)
        if sequence is None:
            raise socket.gaierror(socket.EAI_NONAME, f"{host} does not resolve")
        answer = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return [ipaddress.ip_address(address) for address in answer]
