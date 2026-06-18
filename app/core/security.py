"""Ingress authentication and rate limiting.

The FastAPI front door (app/main.py) is the one untrusted-network surface of the
platform. Everything behind it is already hardened — the read-only ``causal_ro``
DB role, the locked-down R sandbox, redaction, the injection-resistant prompts —
but none of that matters if anyone who can reach the port can spend the LLM + R
budget at will. Two cheap, dependency-free guards close that gap:

- An API-key check (``X-API-Key``). Accepted keys come from env
  (``INGRESS_API_KEYS``); when none are configured the ingress runs OPEN and logs
  a loud warning, so local dev and the test suite keep working unchanged while any
  exposed deployment is one env var away from locked down.
- A fixed-window rate limiter keyed by caller (API key, else client IP). Each
  ``/analyze`` spends real money downstream, so this bounds how fast one caller
  can enqueue analyses. It is in-process and per-worker — sufficient for this
  single-node demo; a multi-replica deployment would back it with Redis (noted,
  not built).
"""
from __future__ import annotations

import logging
import time
from threading import Lock

from fastapi import Header, HTTPException, Request, status

from app.core import config

logger = logging.getLogger(__name__)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject requests without a valid ``X-API-Key`` when keys are configured.

    A no-op when ``INGRESS_API_KEYS`` is empty (see module docstring), so the
    open local-dev posture is explicit rather than accidental.
    """
    if not config.INGRESS_API_KEYS:
        return
    if x_api_key is None or x_api_key not in config.INGRESS_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "API-Key"},
        )


class FixedWindowRateLimiter:
    """Per-caller fixed-window request counter.

    Counts a caller's requests within the current ``window_s`` bucket and rejects
    once the count exceeds ``max_requests``. Fixed-window (not sliding) is
    deliberate: one small tuple per caller and trivially correct, and the brief
    burst allowed across a window boundary does not matter at demo scale. Limits
    are read live on each call so config monkeypatching (tests) takes effect
    without rebuilding the limiter.
    """

    # Bound the caller table so a flood of distinct IPs can't grow it without end;
    # stale-window entries are dropped first since they no longer count.
    _MAX_TRACKED = 4096

    def __init__(self) -> None:
        self._lock = Lock()
        self._hits: dict[str, tuple[int, int]] = {}  # caller -> (window_start, count)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def check(self, caller: str, max_requests: int, window_s: int) -> None:
        if max_requests <= 0:
            return
        now = int(time.time())
        window_start = now - (now % window_s)
        with self._lock:
            start, count = self._hits.get(caller, (window_start, 0))
            if start != window_start:
                start, count = window_start, 0
            count += 1
            self._hits[caller] = (start, count)
            if len(self._hits) > self._MAX_TRACKED:
                self._prune(window_start)
            over = count > max_requests
        if over:
            retry_after = window_s - (now % window_s)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded; slow down.",
                headers={"Retry-After": str(retry_after)},
            )

    def _prune(self, window_start: int) -> None:
        """Drop callers whose last hit was in an earlier window. Caller holds the lock."""
        stale = [c for c, (start, _) in self._hits.items() if start != window_start]
        for c in stale:
            del self._hits[c]


_limiter = FixedWindowRateLimiter()


def reset_rate_limiter() -> None:
    """Clear all rate-limit state. For tests."""
    _limiter.reset()


def rate_limit(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Apply the shared rate limit, keyed by API key when present else client IP."""
    caller = x_api_key or (request.client.host if request.client else "unknown")
    _limiter.check(caller, config.RATE_LIMIT_REQUESTS, config.RATE_LIMIT_WINDOW_S)
