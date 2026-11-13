"""Which IP addresses outbound requests may connect to.

An address is refused when it is not on the public internet: private, loopback, link-local (which
holds cloud metadata services), shared, reserved, multicast, documentation, or an IPv6 form that
embeds or translates to an IPv4 address. Checking the resolved address, not the URL's text, makes
every spelling of a host (127.1, 0x7f000001, 2130706433) reach the same decision.
"""

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

BLOCKED_NETWORKS: tuple[tuple[IPNetwork, str], ...] = tuple(
    (ipaddress.ip_network(network), reason)
    for network, reason in (
        ("0.0.0.0/8", "an unspecified address"),
        ("10.0.0.0/8", "a private address"),
        ("100.64.0.0/10", "a carrier-grade NAT address"),
        ("127.0.0.0/8", "a loopback address"),
        ("169.254.0.0/16", "a link-local address, where cloud metadata services live"),
        ("172.16.0.0/12", "a private address"),
        ("192.0.0.0/24", "an IETF protocol assignment"),
        ("192.0.2.0/24", "a documentation address"),
        ("192.168.0.0/16", "a private address"),
        ("198.18.0.0/15", "a benchmarking address"),
        ("198.51.100.0/24", "a documentation address"),
        ("203.0.113.0/24", "a documentation address"),
        ("224.0.0.0/4", "a multicast address"),
        ("240.0.0.0/4", "a reserved address"),
        ("::/128", "an unspecified address"),
        ("::1/128", "a loopback address"),
        ("64:ff9b::/96", "a NAT64 address, which translates to an IPv4 address"),
        ("100::/64", "a discard-only address"),
        ("2001:db8::/32", "a documentation address"),
        ("2002::/16", "a 6to4 address, which embeds an IPv4 address"),
        ("fc00::/7", "a unique local address"),
        ("fe80::/10", "a link-local address"),
        ("ff00::/8", "a multicast address"),
    )
)
# All internet unicast lies in 2000::/3. Anything outside it that no listed network names, such as
# IPv4-compatible (::/96), IPv4-translated (::ffff:0:0:0/96) or site-local (fec0::/10) forms, can
# embed or reach an internal address, and Python's is_global wrongly calls some of them global.
IPV6_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")
OUTSIDE_GLOBAL_UNICAST_REASON = "outside the IPv6 global unicast range"
# For an address in no listed network that the ipaddress module still knows is not public.
NOT_PUBLIC_REASON = "not a public address"


def refusal_reason(address: IPAddress) -> str | None:
    """Why an outbound connection to ``address`` is refused, or None when it is allowed."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 reaches 127.0.0.1, so it gets the IPv4 address's verdict.
        address = address.ipv4_mapped
    for network, reason in BLOCKED_NETWORKS:
        if address in network:
            return reason
    if isinstance(address, ipaddress.IPv6Address) and address not in IPV6_GLOBAL_UNICAST:
        return OUTSIDE_GLOBAL_UNICAST_REASON
    if not address.is_global:
        return NOT_PUBLIC_REASON
    return None
