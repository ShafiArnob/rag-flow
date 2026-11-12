import asyncio
import threading

import argon2
import pytest
from argon2.exceptions import InvalidHashError, VerificationError

from app.passwords import MAX_CONCURRENT_OPERATIONS, PasswordHasher

PASSWORD = "correct horse battery staple"  # noqa: S105 - a fake value for hashing tests
FAKE_HASH = "fake-argon2-hash"
# Long enough for a hash that is free to start to have started; a hash that is held back never does.
START_TIMEOUT_SECONDS = 1.0


class BlockingArgon2Hash:
    """Stands in for Argon2's hash: holds its thread until released, so a test controls how long
    each hash runs, and records the most that ran at once."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.release = threading.Event()
        self.started: asyncio.Queue[None] = asyncio.Queue()
        self.peak_running = 0
        self._loop = loop
        self._running = 0
        self._lock = threading.Lock()

    def __call__(self, _password: str, **_: object) -> str:
        with self._lock:
            self._running += 1
            self.peak_running = max(self.peak_running, self._running)
        self._loop.call_soon_threadsafe(self.started.put_nowait, None)
        self.release.wait()
        with self._lock:
            self._running -= 1
        return FAKE_HASH


@pytest.fixture
def password_hasher() -> PasswordHasher:
    return PasswordHasher()


async def test_hash_is_argon2id_with_64_mib_and_time_cost_3(
    password_hasher: PasswordHasher,
) -> None:
    password_hash = await password_hasher.hash(PASSWORD)

    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,")
    assert PASSWORD not in password_hash


async def test_verify_accepts_the_password_and_rejects_another(
    password_hasher: PasswordHasher,
) -> None:
    password_hash = await password_hasher.hash(PASSWORD)

    assert await password_hasher.verify(password_hash, PASSWORD) is True
    assert await password_hasher.verify(password_hash, PASSWORD + "!") is False


async def test_hashes_of_one_password_differ(password_hasher: PasswordHasher) -> None:
    assert await password_hasher.hash(PASSWORD) != await password_hasher.hash(PASSWORD)


async def test_verify_without_a_stored_hash_is_false_after_a_real_argon2_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The work is what keeps an unknown email as slow as a wrong password, so it is checked; and
    the answer is False even when that verify matches."""
    password_hasher = PasswordHasher()
    verified: list[tuple[str, str]] = []

    def matching_verify(_: argon2.PasswordHasher, password_hash: str, password: str) -> bool:
        verified.append((password_hash, password))
        return True

    monkeypatch.setattr(argon2.PasswordHasher, "verify", matching_verify)

    assert await password_hasher.verify(None, PASSWORD) is False
    [(dummy_hash, password)] = verified
    assert dummy_hash.startswith("$argon2id$v=19$m=65536,t=3,")
    assert password == PASSWORD


async def test_verify_without_a_stored_hash_rejects_any_password(
    password_hasher: PasswordHasher,
) -> None:
    assert await password_hasher.verify(None, PASSWORD) is False
    assert await password_hasher.verify(None, "") is False


@pytest.mark.parametrize(
    "corrupt_hash",
    ["", "not-a-hash", "$argon2id$v=19$m=65536,t=3,p=4$not-base64$not-base64"],
)
async def test_verify_raises_on_a_corrupt_hash_instead_of_reporting_a_mismatch(
    password_hasher: PasswordHasher, corrupt_hash: str
) -> None:
    with pytest.raises((InvalidHashError, VerificationError)):
        await password_hasher.verify(corrupt_hash, PASSWORD)


async def test_a_cancelled_hash_holds_its_slot_until_its_thread_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread cannot be stopped, so a hash whose caller gave up still holds its memory; letting
    another hash start then would exceed the cap."""
    # Built before the patch, because building it hashes a dummy secret.
    password_hasher = PasswordHasher()
    blocking_hash = BlockingArgon2Hash(asyncio.get_running_loop())
    monkeypatch.setattr(argon2.PasswordHasher, "hash", blocking_hash)
    try:
        abandoned = [
            asyncio.create_task(password_hasher.hash(PASSWORD))
            for _ in range(MAX_CONCURRENT_OPERATIONS)
        ]
        for _ in abandoned:
            await asyncio.wait_for(blocking_hash.started.get(), START_TIMEOUT_SECONDS)
        for task in abandoned:
            task.cancel()
        await asyncio.gather(*abandoned, return_exceptions=True)

        waiting = asyncio.create_task(password_hasher.hash(PASSWORD))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(blocking_hash.started.get(), START_TIMEOUT_SECONDS)

        blocking_hash.release.set()
        assert await waiting == FAKE_HASH
        assert blocking_hash.peak_running == MAX_CONCURRENT_OPERATIONS
    finally:
        blocking_hash.release.set()
