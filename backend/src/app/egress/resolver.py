"""Host name resolution for outbound requests."""

import asyncio
import functools
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

from app.egress.address_policy import IPAddress

# Bounds the lookups one resolver runs at once; a lookup a slow DNS server never answers holds its
# thread until the system resolver gives up.
DEFAULT_MAX_CONCURRENT_LOOKUPS = 8

# Resolves a host name and port to IP addresses, raising socket.gaierror when the name has none.
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]


class SystemResolver:
    """Resolves names with the system resolver on threads of its own.

    A deadline cannot interrupt a lookup that is already running, so lookups run here rather than
    on the event loop's shared executor, which slow lookups would otherwise exhaust for everything
    else in the process.
    """

    def __init__(self, max_concurrent_lookups: int = DEFAULT_MAX_CONCURRENT_LOOKUPS) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_lookups, thread_name_prefix="egress-dns"
        )

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        lookup = functools.partial(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
            # No IPv6 answers on a host without IPv6, where the pinned address would be unreachable.
            flags=socket.AI_ADDRCONFIG,
        )
        addresses = await asyncio.get_running_loop().run_in_executor(self._executor, lookup)
        return [ipaddress.ip_address(sockaddr[0]) for *_, sockaddr in addresses]
