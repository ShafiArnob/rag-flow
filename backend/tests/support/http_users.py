"""Users signed up through the API, each holding its own client and session cookie, for tests that
check one user cannot reach another's data over HTTP."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.identity.sessions import SessionCookie
from support.auth_client import client_for, new_peer_address, sign_up


@dataclass(frozen=True)
class HttpUser:
    client: httpx.AsyncClient
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    csrf_token: str
    session_cookie: SessionCookie


@dataclass(frozen=True)
class TwoUsers:
    first: HttpUser
    second: HttpUser


@asynccontextmanager
async def signed_up_over_http(engine: AsyncEngine, redis: Redis) -> AsyncIterator[HttpUser]:
    """A new user signed up through POST /auth/signup, from a peer address no other client uses,
    whose client keeps the session cookie the signup set."""
    async with client_for(engine, redis, peer_address=new_peer_address()) as client:
        signed_up = await sign_up(client)
        yield HttpUser(
            client=client,
            user_id=signed_up.user_id,
            workspace_id=signed_up.workspace_id,
            csrf_token=signed_up.csrf_token,
            session_cookie=signed_up.cookie,
        )
