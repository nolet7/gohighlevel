"""Shared resilient HTTP: bounded retries with backoff + jitter, honours 429 reset hints."""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class UpstreamError(RuntimeError):
    def __init__(self, service: str, status: int | None, detail: str):
        super().__init__(f"{service} error status={status} detail={detail[:200]}")
        self.service, self.status = service, status


def _retry_delay(resp: httpx.Response | None, attempt: int, now: Callable[[], float]) -> float:
    if resp is not None:
        ra = resp.headers.get("Retry-After")
        if ra and ra.isdigit():
            return min(float(ra), 30.0)
        reset = resp.headers.get("RateLimit-Reset")      # FieldPulse: epoch seconds
        if reset and reset.isdigit():
            return min(max(float(reset) - now(), 0.5), 30.0)
    return min(0.5 * (2 ** attempt), 8.0) + random.uniform(0, 0.25)


def request_with_retry(client: httpx.Client, service: str, method: str, url: str, *,
                       attempts: int = 4, sleep: Callable[[float], None] = time.sleep,
                       now: Callable[[], float] = time.time, **kwargs) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(attempts):
        resp = None
        try:
            resp = client.request(method, url, **kwargs)
            if resp.status_code not in RETRYABLE_STATUS:
                if resp.status_code >= 400:
                    raise UpstreamError(service, resp.status_code, resp.text)
                return resp
            last = UpstreamError(service, resp.status_code, resp.text)
        except httpx.TransportError as e:
            last = e
        if attempt < attempts - 1:
            delay = _retry_delay(resp, attempt, now)
            logger.warning("%s %s retry %d in %.1fs", service, url, attempt + 1, delay)
            sleep(delay)
    if isinstance(last, UpstreamError):
        raise last
    raise UpstreamError(service, None, f"transport failure: {last}")
