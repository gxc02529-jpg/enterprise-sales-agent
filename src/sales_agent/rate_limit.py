from __future__ import annotations

import asyncio
import hashlib
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from math import ceil
from time import monotonic
from typing import Any
from uuid import uuid4


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(1, ceil(retry_after_seconds))
        super().__init__("request rate limit exceeded")


class ApiConcurrencyExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class AdmissionPolicy:
    requests_per_minute: int
    max_concurrent_per_identity: int
    concurrency_wait_seconds: float


class AdmissionController(ABC):
    @abstractmethod
    def admit(self, key: str) -> Any: ...

    @abstractmethod
    def snapshot(self) -> dict[str, int | str]: ...

    async def close(self) -> None:
        return None


class InMemoryAdmissionController(AdmissionController):
    """Per-instance sliding-window rate limiter plus per-identity concurrency guard."""

    def __init__(self, policy: AdmissionPolicy) -> None:
        self.policy = policy
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._semaphores: dict[str, asyncio.BoundedSemaphore] = {}
        self._lock = asyncio.Lock()

    async def _consume_rate(self, key: str) -> None:
        now = monotonic()
        cutoff = now - 60
        async with self._lock:
            bucket = self._requests[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.policy.requests_per_minute:
                raise RateLimitExceeded(60 - (now - bucket[0]))
            bucket.append(now)

    async def _semaphore(self, key: str) -> asyncio.BoundedSemaphore:
        async with self._lock:
            semaphore = self._semaphores.get(key)
            if semaphore is None:
                semaphore = asyncio.BoundedSemaphore(self.policy.max_concurrent_per_identity)
                self._semaphores[key] = semaphore
            return semaphore

    @asynccontextmanager
    async def admit(self, key: str) -> AsyncIterator[None]:
        await self._consume_rate(key)
        semaphore = await self._semaphore(key)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    semaphore.acquire(), timeout=self.policy.concurrency_wait_seconds
                )
                acquired = True
            except TimeoutError as exc:
                raise ApiConcurrencyExceeded("too many concurrent requests") from exc
            yield
        finally:
            if acquired:
                semaphore.release()

    def snapshot(self) -> dict[str, int | str]:
        return {
            "backend": "memory",
            "requests_per_minute": self.policy.requests_per_minute,
            "max_concurrent_per_identity": self.policy.max_concurrent_per_identity,
            "tracked_identities": len(self._requests),
        }


_RATE_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_start = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = 1000
  if oldest[2] then retry = math.max(1, tonumber(oldest[2]) + 60000 - now) end
  return {0, retry}
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, 61000)
return {1, 0}
"""

_CONCURRENCY_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local expires = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZCARD', key) >= limit then return 0 end
redis.call('ZADD', key, expires, member)
redis.call('PEXPIRE', key, math.max(1000, expires - now + 1000))
return 1
"""


class RedisAdmissionController(AdmissionController):
    """Atomic cross-instance rate and concurrency admission using Redis sorted sets."""

    def __init__(
        self,
        redis_url: str,
        policy: AdmissionPolicy,
        *,
        prefix: str,
        client: Any | None = None,
    ) -> None:
        self.policy = policy
        self.prefix = prefix.rstrip(":")
        if client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as exc:
                raise RuntimeError("Redis admission requires: pip install '.[infra]'") from exc
            client = Redis.from_url(redis_url, decode_responses=False)
        self.client = client

    def _key(self, kind: str, identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{self.prefix}:{kind}:{digest}"

    async def _consume_rate(self, identity: str) -> None:
        now_ms = int(time.time() * 1000)
        result = await self.client.eval(
            _RATE_SCRIPT,
            1,
            self._key("rate", identity),
            now_ms,
            now_ms - 60_000,
            self.policy.requests_per_minute,
            uuid4().hex,
        )
        if int(result[0]) != 1:
            raise RateLimitExceeded(int(result[1]) / 1000)

    async def _acquire_concurrency(self, identity: str, lease: str) -> bool:
        now_ms = int(time.time() * 1000)
        # The lease outlives the API deadline and self-heals after process crashes.
        lease_ms = max(60_000, int(self.policy.concurrency_wait_seconds * 1000) + 300_000)
        result = await self.client.eval(
            _CONCURRENCY_SCRIPT,
            1,
            self._key("concurrency", identity),
            now_ms,
            now_ms + lease_ms,
            self.policy.max_concurrent_per_identity,
            lease,
        )
        return int(result) == 1

    @asynccontextmanager
    async def admit(self, key: str) -> AsyncIterator[None]:
        await self._consume_rate(key)
        lease = uuid4().hex
        deadline = monotonic() + self.policy.concurrency_wait_seconds
        acquired = False
        try:
            while monotonic() < deadline:
                if await self._acquire_concurrency(key, lease):
                    acquired = True
                    break
                await asyncio.sleep(min(0.02, self.policy.concurrency_wait_seconds))
            if not acquired:
                raise ApiConcurrencyExceeded("too many concurrent requests")
            yield
        finally:
            if acquired:
                await self.client.zrem(self._key("concurrency", key), lease)

    def snapshot(self) -> dict[str, int | str]:
        return {
            "backend": "redis",
            "requests_per_minute": self.policy.requests_per_minute,
            "max_concurrent_per_identity": self.policy.max_concurrent_per_identity,
            "key_prefix": self.prefix,
        }

    async def close(self) -> None:
        await self.client.aclose()


def build_admission_controller(settings: Any) -> AdmissionController:
    policy = AdmissionPolicy(
        requests_per_minute=settings.api_rate_limit_per_minute,
        max_concurrent_per_identity=settings.api_max_concurrent_per_user,
        concurrency_wait_seconds=settings.api_concurrency_wait_seconds,
    )
    if settings.api_rate_limit_backend == "redis":
        return RedisAdmissionController(
            settings.redis_url,
            policy,
            prefix=settings.api_rate_limit_redis_prefix,
        )
    return InMemoryAdmissionController(policy)
