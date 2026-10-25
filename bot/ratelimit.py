from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, Optional


class RateLimiter:
    """Sliding window در حافظه (برای چند نمونه، به Redis منتقل شود)."""

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = {}

    def allow(self, key: str, limit: int, window: float, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        dq = self._hits.setdefault(key, deque())
        while dq and now - dq[0] >= window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    def gc(self, window: float = 3600.0) -> None:
        now = time.monotonic()
        for key in [k for k, d in self._hits.items() if not d or now - d[-1] > window]:
            del self._hits[key]
