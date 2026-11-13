"""Which addresses outbound requests may connect to."""

import ipaddress

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.egress.address_policy import BLOCKED_NETWORKS, NOT_PUBLIC_REASON, refusal_reason

BLOCKED_IPV4_NETWORKS = [
    network for network, _ in BLOCKED_NETWORKS if isinstance(network, ipaddress.IPv4Network)
]


@pytest.mark.parametrize(
    ("address", "reason_fragment"),
    [
        ("169.254.169.254", "metadata"),
        ("127.0.0.1", "loopback"),
        ("10.1.2.3", "private"),
        ("172.16.0.1", "private"),
        ("192.168.1.1", "private"),
        ("100.64.0.1", "carrier-grade NAT"),
        ("0.0.0.0", "unspecified"),  # noqa: S104 - an address under test, not a bind
        ("224.0.0.1", "multicast"),
        ("255.255.255.255", "reserved"),
        ("::1", "loopback"),
        ("::", "unspecified"),
        ("fc00::1", "unique local"),
        ("fd00:ec2::254", "unique local"),
        ("fe80::1", "link-local"),
        ("ff02::1", "multicast"),
        ("::ffff:127.0.0.1", "loopback"),
        ("::ffff:169.254.169.254", "metadata"),
        ("64:ff9b::7f00:1", "NAT64"),
        ("2002:7f00:1::", "6to4"),
        ("2001:db8::1", "documentation"),
        ("::7f00:1", "global unicast"),
        ("::a9fe:a9fe", "global unicast"),
        ("::ffff:0:7f00:1", "global unicast"),
        ("fec0::1", "global unicast"),
    ],
)
def test_internal_addresses_are_refused_with_their_kind(address: str, reason_fragment: str) -> None:
    reason = refusal_reason(ipaddress.ip_address(address))

    assert reason is not None
    assert reason_fragment in reason


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.215.14", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert refusal_reason(ipaddress.ip_address(address)) is None


def test_an_address_in_no_listed_network_that_is_not_public_is_still_refused() -> None:
    # 2001::/23 (IETF protocol assignments) is not listed, but is not global.
    assert refusal_reason(ipaddress.ip_address("2001::1")) == NOT_PUBLIC_REASON


blocked_ipv4_addresses = st.sampled_from(BLOCKED_IPV4_NETWORKS).flatmap(
    lambda network: st.ip_addresses(network=network)
)


@given(blocked_ipv4_addresses)
def test_every_address_in_a_blocked_ipv4_network_is_refused(
    address: ipaddress.IPv4Address,
) -> None:
    assert refusal_reason(address) is not None


@given(blocked_ipv4_addresses)
def test_the_ipv4_mapped_form_of_a_blocked_address_is_refused(
    address: ipaddress.IPv4Address,
) -> None:
    mapped = ipaddress.IPv6Address(f"::ffff:{address}")

    assert refusal_reason(mapped) == refusal_reason(address)
