"""Argon2id password hashing, kept off the event loop and bounded in memory."""

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor

import argon2
from argon2.exceptions import VerifyMismatchError

ARGON2_MEMORY_COST_KIB = 65536
ARGON2_TIME_COST = 3
# Each hash or verify holds ARGON2_MEMORY_COST_KIB for its duration, so this caps the memory that
# concurrent signups and logins can claim.
MAX_CONCURRENT_OPERATIONS = 4
DUMMY_SECRET_BYTES = 32


class PasswordHasher:
    """Hashes and verifies passwords in worker threads, at most MAX_CONCURRENT_OPERATIONS at a time.

    Hashing takes tens of milliseconds of CPU, so never call it while a database transaction is
    open: the transaction would hold its connection for that long.
    """

    def __init__(self) -> None:
        self._argon2 = argon2.PasswordHasher(
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST_KIB,
            type=argon2.Type.ID,
        )
        # The pool's size is the cap. A semaphore held by the awaiting task would not do: a
        # cancelled task frees its slot while its thread, which cannot be stopped, still runs.
        self._threads = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_OPERATIONS, thread_name_prefix="argon2"
        )
        # Verified against when there is no stored hash, so that case costs what a real one does.
        self._dummy_hash = self._argon2.hash(secrets.token_urlsafe(DUMMY_SECRET_BYTES))

    async def hash(self, password: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._threads, self._argon2.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        """False when the password does not match. A stored hash that cannot be read raises: it
        means corrupt data, not a wrong password.

        ``password_hash`` is None for an unknown account or one without a password. That is always
        False, after the same work as a real verify, so timing does not tell the cases apart.
        """
        loop = asyncio.get_running_loop()
        try:
            # Argon2's verify returns only True; a mismatch raises.
            await loop.run_in_executor(
                self._threads,
                self._argon2.verify,
                self._dummy_hash if password_hash is None else password_hash,
                password,
            )
        except VerifyMismatchError:
            return False
        return password_hash is not None
