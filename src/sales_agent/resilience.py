from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from random import random
from time import monotonic
from typing import TypeVar

import httpx

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    def __init__(self, name: str, retry_after_seconds: float) -> None:
        self.name = name
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(f"circuit '{name}' is open")


class BulkheadFullError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResiliencePolicy:
    timeout_seconds: float
    max_retries: int = 1
    retry_base_delay_seconds: float = 0.2
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30
    max_concurrency: int = 20
    bulkhead_wait_seconds: float = 0.1


class AsyncCircuitBreaker:
    """A per-dependency circuit breaker with one half-open probe."""

    def __init__(self, name: str, *, failure_threshold: int, recovery_timeout: float) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_probe = False
        self._lock = asyncio.Lock()

    async def before_call(self) -> None:
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return
            now = monotonic()
            if self._state == CircuitState.OPEN:
                elapsed = now - (self._opened_at or now)
                if elapsed < self.recovery_timeout:
                    raise CircuitOpenError(self.name, self.recovery_timeout - elapsed)
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe = False
            if self._half_open_probe:
                raise CircuitOpenError(self.name, self.recovery_timeout)
            self._half_open_probe = True

    async def record_success(self) -> None:
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None
            self._half_open_probe = False

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = monotonic()
            self._half_open_probe = False

    def snapshot(self) -> dict[str, str | int | float | None]:
        retry_after: float | None = None
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            retry_after = max(0.0, self.recovery_timeout - (monotonic() - self._opened_at))
        return {
            "name": self.name,
            "state": self._state.value,
            "consecutive_failures": self._failures,
            "retry_after_seconds": round(retry_after, 3) if retry_after is not None else None,
        }


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (PermissionError, ValueError, TypeError, CircuitOpenError)):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429} or exc.response.status_code >= 500
    return isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError, RuntimeError))


class ResilientExecutor:
    """Combines timeout, bounded concurrency, retry and circuit breaking."""

    def __init__(self, name: str, policy: ResiliencePolicy) -> None:
        self.name = name
        self.policy = policy
        self.breaker = AsyncCircuitBreaker(
            name,
            failure_threshold=policy.failure_threshold,
            recovery_timeout=policy.recovery_timeout_seconds,
        )
        self._semaphore = asyncio.BoundedSemaphore(policy.max_concurrency)

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        retryable: bool = True,
    ) -> T:
        await self.breaker.before_call()
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=self.policy.bulkhead_wait_seconds
                )
                acquired = True
            except TimeoutError as exc:
                raise BulkheadFullError(f"bulkhead '{self.name}' has no capacity") from exc

            last_error: Exception | None = None
            for attempt in range(self.policy.max_retries + 1):
                try:
                    async with asyncio.timeout(self.policy.timeout_seconds):
                        result = await operation()
                    await self.breaker.record_success()
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    should_retry = (
                        retryable
                        and attempt < self.policy.max_retries
                        and is_retryable_exception(exc)
                    )
                    if not should_retry:
                        break
                    delay = self.policy.retry_base_delay_seconds * (2**attempt)
                    await asyncio.sleep(delay * (0.8 + random() * 0.4))
            await self.breaker.record_failure()
            assert last_error is not None
            raise last_error
        except BulkheadFullError:
            # Saturation is load shedding, not a downstream dependency failure.
            raise
        finally:
            if acquired:
                self._semaphore.release()

    def snapshot(self) -> dict[str, object]:
        return {
            **self.breaker.snapshot(),
            "timeout_seconds": self.policy.timeout_seconds,
            "max_retries": self.policy.max_retries,
            "max_concurrency": self.policy.max_concurrency,
        }
