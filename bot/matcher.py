from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import delete, or_, select, true, update

from database.models import ActiveChat, QueueEntry, User, utcnow
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
    """تطبیق اتمیک: قفل داخل پردازش + آپدیت شرطی در دیتابیس."""

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
        async with self.db.tx() as s:
            me = await s.get(User, user_id)
            if me is None or me.gender not in GENDERS or me.want not in WANTS:
                return JoinResult("invalid")
            if me.state == "chatting":
                return JoinResult("busy")
            if await s.get(QueueEntry, user_id) is not None:
                return JoinResult("queued")  # درخواست تکراری؛ زمان انتظار ریست نمی‌شود
            now = utcnow()
            me.state = "waiting"
            me.state_data = None
            s.add(QueueEntry(user_id=user_id, gender=me.gender, want=me.want,
                             enqueued_at=now, expires_at=now + timedelta(seconds=timeout)))
            await s.flush()

            want_ok = true() if me.want == "any" else (QueueEntry.gender == me.want)
            cand = (await s.execute(
                select(QueueEntry)
                .where(QueueEntry.user_id != user_id, QueueEntry.expires_at > now, want_ok,
                       or_(QueueEntry.want == "any", QueueEntry.want == me.gender))
                .order_by(QueueEntry.enqueued_at)
                .limit(1).with_for_update(skip_locked=True)
            )).scalars().first()
            if cand is None:
                return JoinResult("queued")

            r1 = await s.execute(delete(QueueEntry).where(QueueEntry.user_id == cand.user_id))
            r2 = await s.execute(delete(QueueEntry).where(QueueEntry.user_id == user_id))
            if r1.rowcount != 1 or r2.rowcount != 1:
                raise _ClaimFailed()
            r3 = await s.execute(
                update(User).where(User.id.in_([user_id, cand.user_id]), User.state == "waiting")
                .values(state="chatting", state_data=None, updated_at=utcnow())
            )
            if r3.rowcount != 2:
                raise _ClaimFailed()
            chat = ActiveChat(user_a=user_id, user_b=cand.user_id)
            s.add(chat)
            await s.flush()
            return JoinResult("matched", chat.id, cand.user_id)

    async def leave(self, user_id: int) -> bool:
        async with self._lock:
            async with self.db.tx() as s:
                r = await s.execute(delete(QueueEntry).where(QueueEntry.user_id == user_id))
                await s.execute(update(User).where(User.id == user_id, User.state == "waiting")
                                .values(state="idle", updated_at=utcnow()))
                return bool(r.rowcount)

    async def sweep(self, now: Optional[datetime] = None) -> List[int]:
        """کاربران منقضی‌شده را از صف خارج می‌کند و شناسه‌شان را برمی‌گرداند."""
        now = now or utcnow()
        expired: List[int] = []
        async with self._lock:
            async with self.db.tx() as s:
                ids = (await s.execute(
                    select(QueueEntry.user_id).where(QueueEntry.expires_at <= now)
                )).scalars().all()
                for uid in ids:
                    r = await s.execute(delete(QueueEntry).where(QueueEntry.user_id == uid))
                    if r.rowcount:
                        await s.execute(update(User).where(User.id == uid, User.state == "waiting")
                                        .values(state="idle", updated_at=utcnow()))
                        expired.append(uid)
        return expired

    async def repair(self) -> None:
        """پس از ری‌استارت: وضعیت‌های ناسازگار را به حالت امن برمی‌گرداند."""
        async with self.db.tx() as s:
            in_queue = select(QueueEntry.user_id)
            await s.execute(update(User).where(User.state == "waiting", User.id.notin_(in_queue))
                            .values(state="idle"))
            a = select(ActiveChat.user_a).where(ActiveChat.active.is_(True))
            b = select(ActiveChat.user_b).where(ActiveChat.active.is_(True))
            await s.execute(update(User).where(User.state == "chatting", User.id.notin_(a),
                                               User.id.notin_(b)).values(state="idle"))
