"""One user cannot reach another's data through the API. Two users sign up over HTTP, and routes are
called as one of them with the other's ids and the other's session parts.

CROSS_TENANT_PROBES must name every route outside PUBLIC_ROUTES, so a new route cannot be added
without a probe, or a stated reason its requests name no ids another user could own. That each such
route demands a session, and a CSRF token when it can change state, is checked in
test_route_protection.py.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.authentication import CSRF_HEADER_NAME
from app.sessions import SessionCookie, encode_session_cookie
from auth_client import (
    assert_csrf_token_invalid,
    assert_not_authenticated,
    client_for,
    cookie_header,
    new_peer_address,
)
from http_users import HttpUser, TwoUsers
from public_routes import protected_routes


@dataclass(frozen=True)
class TargetIds:
    """The ids a probe puts in its request: another user's, or random ones nobody owns."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID


@dataclass(frozen=True)
class NoForeignIds:
    """Marks a route whose requests name nothing but the caller's own session."""

    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("NoForeignIds needs the reason the route's requests name no ids")


# Sends one route's request as `caller`, naming the target's ids wherever the route takes an id.
CrossTenantProbe = Callable[[HttpUser, TargetIds], Awaitable[httpx.Response]]

CROSS_TENANT_PROBES: dict[tuple[str, str], CrossTenantProbe | NoForeignIds] = {
    ("POST", "/auth/logout"): NoForeignIds(
        "it ends the session the caller's cookie names; the cookie splice and CSRF tests here"
        " cover the session parts another user owns"
    ),
    ("GET", "/me"): NoForeignIds(
        "it answers with the identity the caller's cookie names and takes no ids"
    ),
}


def ids_of(user: HttpUser) -> TargetIds:
    return TargetIds(user_id=user.user_id, workspace_id=user.workspace_id)


def random_ids() -> TargetIds:
    return TargetIds(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())


def both_ways(users: TwoUsers) -> tuple[tuple[HttpUser, HttpUser], ...]:
    return ((users.first, users.second), (users.second, users.first))


async def probe_failure(probe: CrossTenantProbe, caller: HttpUser, owner: HttpUser) -> str | None:
    """Why ``probe`` shows ``caller`` something of ``owner``'s, or None when it shows nothing.

    The request naming the owner's ids must get 404 with the same body as one naming random ids,
    so the response cannot even tell an id another user owns from one that does not exist.
    """
    foreign = await probe(caller, ids_of(owner))
    unowned = await probe(caller, random_ids())
    if foreign.status_code != 404:
        return f"{foreign.status_code} for the other user's ids: {foreign.text}"
    if unowned.status_code != 404:
        return f"{unowned.status_code} for random ids, so the probe is wrong: {unowned.text}"
    if foreign.content != unowned.content:
        return f"404 bodies differ: {foreign.text} for the other user's, {unowned.text} for random"
    return None


async def assert_me_answers_only_with_own_ids(user: HttpUser, other: HttpUser) -> None:
    response = await user.client.get("/me")

    assert response.status_code == 200
    body = response.json()
    assert (body["user"]["id"], body["workspace"]["id"], body["csrf_token"]) == (
        str(user.user_id),
        str(user.workspace_id),
        user.csrf_token,
    )
    for foreign in (str(other.user_id), str(other.workspace_id), other.csrf_token):
        assert foreign not in response.text


def test_every_protected_route_has_a_probe_or_a_reason_it_needs_none(
    app_without_database: FastAPI,
) -> None:
    protected = set(protected_routes(app_without_database))
    registered = set(CROSS_TENANT_PROBES)

    assert sorted(protected - registered) == [], "add each to CROSS_TENANT_PROBES"
    assert sorted(registered - protected) == [], "CROSS_TENANT_PROBES names public or gone routes"


@pytest.mark.integration
async def test_every_probe_gets_the_same_404_for_the_other_users_ids_as_for_random_ones(
    two_users: TwoUsers,
) -> None:
    failures = []
    for (method, path), entry in CROSS_TENANT_PROBES.items():
        if isinstance(entry, NoForeignIds):
            continue
        for caller, owner in both_ways(two_users):
            failure = await probe_failure(entry, caller, owner)
            if failure is not None:
                failures.append(f"{method} {path}: {failure}")

    assert failures == []


async def ask_me_naming_the_target(caller: HttpUser, target: TargetIds) -> httpx.Response:
    # /me ignores query parameters, so it stands in for a route that answers whatever the ids.
    return await caller.client.get("/me", params={"workspace_id": str(target.workspace_id)})


async def request_an_unrouted_path(caller: HttpUser, target: TargetIds) -> httpx.Response:
    return await caller.client.get(f"/unrouted/{target.workspace_id}/{target.user_id}")


@pytest.mark.integration
async def test_a_probe_of_a_route_that_answers_whatever_the_ids_fails(two_users: TwoUsers) -> None:
    """The negative control: probe_failure reports a route that answers 200."""
    failure = await probe_failure(ask_me_naming_the_target, two_users.first, two_users.second)

    assert failure is not None
    assert failure.startswith("200 for the other user's ids")


@pytest.mark.integration
async def test_a_probe_answered_alike_for_the_other_users_ids_and_random_ones_passes(
    two_users: TwoUsers,
) -> None:
    """The positive control: probe_failure accepts the same 404 for both."""
    assert await probe_failure(request_an_unrouted_path, two_users.first, two_users.second) is None


@pytest.mark.integration
async def test_me_answers_each_user_with_their_own_ids_and_none_of_the_others(
    two_users: TwoUsers,
) -> None:
    for user, other in both_ways(two_users):
        await assert_me_answers_only_with_own_ids(user, other)


@pytest.mark.integration
async def test_a_cookie_with_one_users_token_and_the_other_users_workspace_is_401(
    two_users: TwoUsers, app_engine: AsyncEngine, redis: Redis
) -> None:
    async with client_for(app_engine, redis, peer_address=new_peer_address()) as stranger:
        for token_owner, workspace_owner in both_ways(two_users):
            own = encode_session_cookie(token_owner.session_cookie)
            spliced = encode_session_cookie(
                SessionCookie(
                    workspace_id=workspace_owner.workspace_id,
                    token=token_owner.session_cookie.token,
                )
            )
            # The negative control: the token works with its own workspace.
            response = await stranger.get("/me", headers=cookie_header(own))
            assert response.status_code == 200
            assert response.json()["user"]["id"] == str(token_owner.user_id)

            assert_not_authenticated(await stranger.get("/me", headers=cookie_header(spliced)))


@pytest.mark.integration
async def test_logout_with_the_other_users_csrf_token_is_403_and_both_sessions_still_work(
    two_users: TwoUsers,
) -> None:
    for caller, other in both_ways(two_users):
        assert_csrf_token_invalid(
            await caller.client.post("/auth/logout", headers={CSRF_HEADER_NAME: other.csrf_token})
        )

    for user, other in both_ways(two_users):
        await assert_me_answers_only_with_own_ids(user, other)
