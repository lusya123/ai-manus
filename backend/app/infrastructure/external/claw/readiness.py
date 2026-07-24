"""Shared deadline-based readiness polling for Claw runtimes."""

import asyncio

import httpx


async def wait_for_http_health(
    base_url: str,
    *,
    total_timeout_seconds: float,
    request_timeout_seconds: float = 5.0,
    retry_interval_seconds: float = 2.0,
) -> bool:
    """Poll ``/health`` without allowing per-request timeouts to add up."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(total_timeout_seconds))
    async with httpx.AsyncClient(timeout=None) as client:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            request_budget = min(float(request_timeout_seconds), remaining)
            try:
                async with asyncio.timeout(request_budget):
                    response = await client.get(f"{base_url}/health")
                if response.status_code == 200:
                    return True
            except Exception:
                pass

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(float(retry_interval_seconds), remaining))
