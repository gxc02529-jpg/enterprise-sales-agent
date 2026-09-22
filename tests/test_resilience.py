import asyncio

import pytest

from sales_agent.rate_limit import AdmissionPolicy, InMemoryAdmissionController, RateLimitExceeded
from sales_agent.resilience import CircuitOpenError, ResiliencePolicy, ResilientExecutor


@pytest.mark.asyncio
async def test_retry_then_success() -> None:
    calls = 0
    executor = ResilientExecutor(
        "test",
        ResiliencePolicy(
            timeout_seconds=1,
            max_retries=1,
            retry_base_delay_seconds=0,
            failure_threshold=2,
            recovery_timeout_seconds=10,
            max_concurrency=1,
        ),
    )

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")
        return "ok"

    assert await executor.run(operation) == "ok"
    assert calls == 2
    assert executor.snapshot()["state"] == "closed"


@pytest.mark.asyncio
async def test_circuit_opens_and_fails_fast() -> None:
    executor = ResilientExecutor(
        "broken",
        ResiliencePolicy(
            timeout_seconds=1,
            max_retries=0,
            failure_threshold=1,
            recovery_timeout_seconds=60,
            max_concurrency=1,
        ),
    )

    async def operation() -> None:
        raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        await executor.run(operation)
    with pytest.raises(CircuitOpenError):
        await executor.run(operation)


@pytest.mark.asyncio
async def test_operation_deadline_is_enforced() -> None:
    executor = ResilientExecutor(
        "slow",
        ResiliencePolicy(
            timeout_seconds=0.01,
            max_retries=0,
            failure_threshold=1,
            recovery_timeout_seconds=60,
            max_concurrency=1,
        ),
    )

    async def operation() -> None:
        await asyncio.sleep(1)

    with pytest.raises(TimeoutError):
        await executor.run(operation)


@pytest.mark.asyncio
async def test_per_identity_rate_limit() -> None:
    controller = InMemoryAdmissionController(
        AdmissionPolicy(
            requests_per_minute=1,
            max_concurrent_per_identity=1,
            concurrency_wait_seconds=0.01,
        )
    )
    async with controller.admit("tenant:user"):
        pass
    with pytest.raises(RateLimitExceeded):
        async with controller.admit("tenant:user"):
            pass
    async with controller.admit("tenant:other-user"):
        pass
