"""In-memory token buckets per client IP (normal + cron)."""
import time
from typing import Dict, Tuple

from app.config import settings


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, tokens: float):
        self.tokens = float(tokens)
        self.updated = time.monotonic()


class RateLimiter:
    def __init__(self) -> None:
        self._normal: Dict[str, _Bucket] = {}
        self._cron: Dict[str, _Bucket] = {}

    def check_limit(self, ip: str, is_cron: bool) -> Tuple[bool, int]:
        if is_cron:
            cap = settings.RATE_CRON_CAP
            refill = settings.RATE_CRON_REFILL_SEC
            store = self._cron
        else:
            cap = settings.RATE_ENDPOINT_CAP
            refill = settings.RATE_ENDPOINT_REFILL_SEC
            store = self._normal
        cap = max(1, int(cap))
        refill = max(1, int(refill))
        now = time.monotonic()
        b = store.get(ip)
        if b is None:
            b = _Bucket(cap)
            store[ip] = b
        elapsed = now - b.updated
        if elapsed > 0:
            b.tokens = min(cap, b.tokens + elapsed * (cap / refill))
            b.updated = now
        if b.tokens >= 1:
            b.tokens -= 1
            return True, 0
        retry = int((1 - b.tokens) * (refill / cap)) + 1
        return False, max(1, retry)


limiter = RateLimiter()
