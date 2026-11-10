"""Retries with exponential backoff and jitter."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


class TransientError(RuntimeError):
    """A failure that is safe to retry (rate limit, 5xx, connection refused before sending)."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    jitter: float = 0.25

    def delay(self, attempt: int) -> float:
        d = min(self.max_delay_s, self.base_delay_s * (2**attempt))
        return d * (1 + random.uniform(-self.jitter, self.jitter))


def with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy | None = None,
    *,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    policy = policy or RetryPolicy()
    for attempt in range(policy.attempts):
        try:
            return fn()
        except retry_on as e:
            if attempt == policy.attempts - 1:
                raise
            if on_retry:
                on_retry(attempt + 1, e)
            sleep(policy.delay(attempt))
    raise AssertionError("unreachable")
