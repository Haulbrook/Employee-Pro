#!/usr/bin/env python3
"""SENTINEL — rate_limiter.py (Phase B.5).

Token-bucket rate limiter used by ai_engine before each provider call.

The bucket holds up to ``capacity`` tokens and refills at
``refill_per_second`` tokens per second. ``acquire(n)`` blocks until ``n``
tokens are available, never busy-waiting longer than 250 ms between checks.

Defaults are conservative (10 tokens, 0.2/s = 12/min) — override per the
``ai.rate_limit`` block of sentinel.yaml.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, capacity: float = 10, refill_per_second: float = 0.2):
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be > 0")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)

    def try_acquire(self, n: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def acquire(self, n: int = 1, timeout: float | None = None) -> bool:
        """Block until ``n`` tokens are available. Returns True on success,
        False if ``timeout`` was specified and exceeded."""
        if n > self.capacity:
            raise ValueError(f"requested {n} > capacity {self.capacity}")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.try_acquire(n):
                return True
            with self._lock:
                self._refill()
                shortfall = n - self._tokens
                wait = max(0.05, shortfall / self.refill_per_second)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(min(wait, 0.25))

    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def __repr__(self) -> str:
        return (f"TokenBucket(capacity={self.capacity}, "
                f"refill_per_second={self.refill_per_second}, tokens={self._tokens:.2f})")
