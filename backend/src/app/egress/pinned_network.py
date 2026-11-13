"""A network backend that connects only to one address the egress client has already checked."""

import typing

import httpcore

from app.egress.address_policy import IPAddress


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connects every TCP request to ``address``, whatever host name httpcore asks for.

    The name is resolved once, before this backend exists, so a DNS answer that changes between the
    check and the connection (rebinding) is never used. httpcore still passes the URL's host name to
    TLS, so the certificate is checked against the name, not the address.
    """

    def __init__(self, address: IPAddress, inner: httpcore.AsyncNetworkBackend) -> None:
        self._address = address
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_tcp(
            str(self._address),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise RuntimeError("outbound requests never use Unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)
