from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from postgrest.exceptions import APIError

from database.models import iso, utcnow
from database.session import Database
from .settings import SettingsService

log = logging.getLogger("matcher")

GENDERS = ("male", "female")
WANTS = ("male", "female", "any")


@dataclass
class JoinResult:
    status: str  # matched | queued | invalid | busy
    chat_id: Optional[int] = None
    partner_id: Optional[int] = None


class _ClaimFailed(Exception):
    pass


class Matcher:
    """تطبیق اتمیک: قفل داخل پردازش + تابع SQL anon_join که همه‌چیز را در یک تراکنش انجام می‌دهد."""

    def __init__(self, db: Database, settings: SettingsService) -> None:
        self.db = db
        self.settings = settings
        self._lock = asyncio.Lock()

    async def join(self, user_id: int) -> JoinResult:
        timeout = await self.settings.get_int("match_timeout")
        async with self._lock:
            for _ in range(3):
                try:
                    return await self._join_once(user_id, timeout)
                except _ClaimFailed:
                    continue
            return JoinResult("queued")

    async def _join_once(self, user_id: int, timeout: int) -> JoinResult:
        try:
            res = await self.db.rpc("anon_join", {"p_user_id": user_id, "p_timeout": timeout})
        except APIError as exc:
            if "claim_failed" in str(exc):
                raise _ClaimFailed() from exc
            raise
        res = res or {}
        return JoinResult(res.get("status", "invalid"), res.get("chat_id"), res.get("partner_id"))

    async def leave(self, user_id: int) -> bool:
        async with self._lock:
            return bool(await self.db.rpc("anon_leave", {"p_user_id": user_id}))

    async def sweep(self, now: Optional[datetime] = None) -> List[int]:
        """کاربران منقضی‌شده را از صف خارج می‌کند و شناسه‌شان را برمی‌گرداند."""
        now = now or utcnow()
        async with self._lock:
            ids = await self.db.rpc("anon_sweep", {"p_now": iso(now)})
        return [int(i) for i in (ids or [])]

    async def repair(self) -> None:
        """پس از ری‌استارت: وضعیت‌های ناسازگار را به حالت امن برمی‌گرداند."""
        await self.db.rpc("anon_repair")
