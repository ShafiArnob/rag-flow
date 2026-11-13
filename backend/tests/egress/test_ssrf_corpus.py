"""The SSRF corpus: requests the egress client must refuse before connecting anywhere.

Plain HTTP is allowed throughout, so each refusal comes from the address or the scheme, not from
the http/https rule. IP spellings go through the real system resolver, which parses them without
touching the network; hosts that need DNS use FakeResolver.
"""

import pytest

from app.egress.client import EgressClient, EgressRefusedError
from app.egress.resolver import SystemResolver
from support.fake_network import FakeNetwork, FakeResolver, http_response, redirect

PUBLIC_ADDRESS = "93.184.215.14"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://127.1/",
        "http://0x7f000001/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fc00::1]/",
        "http://[fd00:ec2::254]/",
        "http://[::7f00:1]/",
        "http://[::a9fe:a9fe]/",
        "http://[::ffff:0:7f00:1]/",
        "http://[fec0::1]/",
        "http://0.0.0.0:6379/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "https://[fe80::1]/",
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "ftp://example.com/",
    ],
)
async def test_the_request_is_refused_without_connecting(url: str) -> None:
    network = FakeNetwork([])
    client = EgressClient(resolver=SystemResolver(), network_backend=network)

    with pytest.raises(EgressRefusedError):
        await client.request("GET", url, allow_plain_http=True)

    assert network.connections == []


async def test_a_host_that_resolves_to_a_private_address_among_public_ones_is_refused() -> None:
    network = FakeNetwork([])
    resolver = FakeResolver({"rebind.attacker.test": [[PUBLIC_ADDRESS, "127.0.0.1"]]})
    client = EgressClient(resolver=resolver, network_backend=network)

    with pytest.raises(EgressRefusedError) as refused:
        await client.request("GET", "https://rebind.attacker.test/")

    assert refused.value.detail == "127.0.0.1 is a loopback address"
    assert network.connections == []


async def test_a_rebinding_host_is_connected_only_at_its_first_answer() -> None:
    network = FakeNetwork([http_response(200)])
    resolver = FakeResolver({"rebind.attacker.test": [[PUBLIC_ADDRESS], ["127.0.0.1"]]})
    client = EgressClient(resolver=resolver, network_backend=network)

    await client.request("GET", "https://rebind.attacker.test/")

    assert resolver.lookups == ["rebind.attacker.test"]
    assert [connection.host for connection in network.connections] == [PUBLIC_ADDRESS]


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "https://[::1]/",
        "http://internal.attacker.test/",
        "file:///etc/passwd",
    ],
)
async def test_a_redirect_to_an_internal_target_is_refused(location: str) -> None:
    network = FakeNetwork([redirect(302, location)])
    resolver = FakeResolver(
        {"redirector.test": [[PUBLIC_ADDRESS]], "internal.attacker.test": [["10.0.0.7"]]}
    )
    client = EgressClient(resolver=resolver, network_backend=network)

    with pytest.raises(EgressRefusedError):
        await client.request("GET", "https://redirector.test/", allow_plain_http=True)

    assert [connection.host for connection in network.connections] == [PUBLIC_ADDRESS]
