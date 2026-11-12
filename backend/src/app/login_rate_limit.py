"""Login attempt limits, kept in Redis: per client IP address, and per account with a lockout that
doubles each time it recurs.

An attempt is registered before its password is verified, in one Lua script that Redis runs
atomically, so parallel guesses cannot slip past a limit between a check and a count. Redis errors
are never caught: a login that cannot be counted must fail, not go unlimited.
"""

import hashlib
import uuid

from redis.asyncio import Redis

from app.audit import IPAddress

IP_ATTEMPT_LIMIT = 20
ACCOUNT_ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW_SECONDS = 60
FIRST_LOCKOUT_SECONDS = 60
MAXIMUM_LOCKOUT_SECONDS = 900
# How long an account's lockouts keep counting towards the next one's length.
LOCKOUT_COUNT_LIFETIME_SECONDS = 86400
# Clients without a peer address, as over a Unix socket, share one budget rather than having none.
NO_PEER_ADDRESS = "none"

# KEYS: ip attempts, account attempts, account lock, account lockouts.
# ARGV: IP limit, account limit, window, lockout count lifetime, then the lockout durations in
# order, the last of which repeats.
# Returns 0 when the attempt may proceed, else the whole seconds until the client may try again.
# A refused attempt reports at least 1, because 0 means allowed and Redis reports a PTTL of 0 for a
# key in the last millisecond before it expires.
_REGISTER_ATTEMPT = """
local ip_attempts = redis.call('INCR', KEYS[1])
if ip_attempts == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[3])
end
if ip_attempts > tonumber(ARGV[1]) then
  return math.max(1, math.ceil(redis.call('PTTL', KEYS[1]) / 1000))
end
local lock_milliseconds = redis.call('PTTL', KEYS[3])
if lock_milliseconds > 0 then
  return math.ceil(lock_milliseconds / 1000)
end
local attempts = redis.call('INCR', KEYS[2])
if attempts == 1 then
  redis.call('EXPIRE', KEYS[2], ARGV[3])
end
if attempts <= tonumber(ARGV[2]) then
  return 0
end
local lockouts = redis.call('INCR', KEYS[4])
redis.call('EXPIRE', KEYS[4], ARGV[4])
-- The lockout durations start at ARGV[5].
local duration = tonumber(ARGV[math.min(4 + lockouts, #ARGV)])
redis.call('SET', KEYS[3], 1, 'EX', duration)
redis.call('DEL', KEYS[2])
return duration
"""


def lockout_seconds(lockout_count: int) -> int:
    """How long the ``lockout_count``-th lockout of an account lasts, counting from 1."""
    # Annotated because typeshed types int ** int as Any: a negative exponent gives a float.
    doubling: int = 2 ** (lockout_count - 1)
    return min(FIRST_LOCKOUT_SECONDS * doubling, MAXIMUM_LOCKOUT_SECONDS)


def _lockout_schedule() -> list[int]:
    """lockout_seconds for each lockout count up to the first that reaches the cap."""
    schedule = [lockout_seconds(1)]
    while schedule[-1] < MAXIMUM_LOCKOUT_SECONDS:
        schedule.append(lockout_seconds(len(schedule) + 1))
    return schedule


def user_account_key(user_id: uuid.UUID) -> str:
    return f"user:{user_id}"


def email_account_key(email: str) -> str:
    """The key for an email no account has. Hashed, so Redis never holds the address."""
    return f"email:{hashlib.sha256(email.lower().encode()).hexdigest()}"


def _ip_key(ip: IPAddress | None) -> str:
    return f"login:ip:{NO_PEER_ADDRESS if ip is None else ip}"


def _account_keys(account_key: str) -> tuple[str, str, str]:
    """The account's attempts, lock and lockouts keys, in that order."""
    prefix = f"login:account:{account_key}"
    return f"{prefix}:attempts", f"{prefix}:lock", f"{prefix}:lockouts"


class LoginRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._register_attempt = redis.register_script(_REGISTER_ATTEMPT)
        self._arguments = [
            IP_ATTEMPT_LIMIT,
            ACCOUNT_ATTEMPT_LIMIT,
            ATTEMPT_WINDOW_SECONDS,
            LOCKOUT_COUNT_LIFETIME_SECONDS,
            *_lockout_schedule(),
        ]

    async def register_attempt(self, ip: IPAddress | None, account_key: str) -> int | None:
        """Counts a login attempt from ``ip`` on ``account_key``. None when it may proceed, else the
        whole seconds until the client may try again; a limited attempt must not be verified.

        An attempt over the IP limit leaves the account's counts alone, so one address cannot lock
        accounts beyond its own budget.
        """
        attempts, lock, lockouts = _account_keys(account_key)
        retry_after = int(
            await self._register_attempt(
                keys=[_ip_key(ip), attempts, lock, lockouts], args=self._arguments
            )
        )
        return None if retry_after == 0 else retry_after

    async def clear_account(self, account_key: str) -> None:
        """Forgets the account's attempts and lockouts, as after a successful login."""
        await self._redis.delete(*_account_keys(account_key))
