"""A token bucket in front of the provider.

The Gemini free tier allows 15 requests per minute. Exceeding it returns a 429,
and the honest way to handle a known limit is to stay under it rather than to
discover it and back off. Backoff still exists for the case where the limit was
hit anyway, for instance because another process shares the key.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Blocks the caller until a request is allowed. Thread-safe."""

    def __init__(self, requests_per_minute: int) -> None:
        self.requests_per_minute = requests_per_minute
        self._interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self.waited_seconds = 0.0
        self.wait_count = 0

    def acquire(self) -> float:
        """Wait if needed. Returns how long this call was delayed, in seconds."""
        if self._interval <= 0:
            return 0.0

        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._interval

        if delay > 0:
            time.sleep(delay)
            self.waited_seconds += delay
            self.wait_count += 1
        return delay


def backoff_seconds(attempt: int, base: float = 2.0, cap: float = 30.0) -> float:
    """Exponential backoff for a rate limit that was hit despite the bucket.

    `base` of zero disables waiting, which the test suite uses: proving that
    the code waits is not worth spending the suite's runtime asleep.
    """
    if base <= 0:
        return 0.0
    return min(cap, base * (2 ** max(0, attempt - 1)))
