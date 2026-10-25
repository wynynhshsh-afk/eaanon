from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, List

from sqlalchemy import func, insert, select, update

from adapters.base import AdapterError
from database.models import (
    Ban, BroadcastJob, BroadcastRecipient, User, utcnow,
)
from database.session import Database

log = logging.getLogger("ads")

SendFn = Callable[[User, str], Awaitable[None]]


class Advertising:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_draft(self, admin_id: str, text: str) -> int:
        async with self.db.tx() as s:
            job = BroadcastJob(created_by=admin_id, text=text, status="draft")
            s.add(job)
            await s.flush()
            return job.id

    async def audience_size(self) -> int:
        async with self.db.tx() as s:
            return (await s.execute(select(func.count(User.id)).where(
                User.id.notin_(self._banned_ids())))).scalar_one()

    @staticmethod
    def _banned_ids():
        now = utcnow()
        return select(Ban.user_id).where(
            Ban.revoked.is_(False), (Ban.until.is_(None)) | (Ban.until > now))

    async def start(self, job_id: int) -> bool:
        """گیرندگان را یک‌بار می‌سازد (unique) و کار را running می‌کند."""
        async with self.db.tx() as s:
            job = await s.get(BroadcastJob, job_id)
            if job is None or job.status != "draft":
                return False
            ids = (await s.execute(select(User.id).where(
                User.id.notin_(self._banned_ids())))).scalars().all()
            for i in range(0, len(ids), 500):
                await s.execute(insert(BroadcastRecipient), [
                    {"job_id": job_id, "user_id": uid, "status": "pending",
                     "created_at": utcnow(), "updated_at": utcnow()}
                    for uid in ids[i:i + 500]])
            job.status = "running"
            return True

    async def cancel(self, job_id: int) -> None:
        async with self.db.tx() as s:
            await s.execute(update(BroadcastJob).where(
                BroadcastJob.id == job_id, BroadcastJob.status.in_(("draft", "running")))
                .values(status="cancelled", updated_at=utcnow()))

    async def running_jobs(self) -> List[int]:
        async with self.db.tx() as s:
            return list((await s.execute(select(BroadcastJob.id).where(
                BroadcastJob.status == "running"))).scalars().all())

    async def counts(self, job_id: int) -> Dict[str, int]:
        async with self.db.tx() as s:
            rows = (await s.execute(select(BroadcastRecipient.status, func.count())
                                    .where(BroadcastRecipient.job_id == job_id)
                                    .group_by(BroadcastRecipient.status))).all()
        out = {"pending": 0, "sent": 0, "failed": 0, "blocked": 0}
        out.update({k: v for k, v in rows})
        return out

    async def status(self, job_id: int) -> str:
        async with self.db.tx() as s:
            job = await s.get(BroadcastJob, job_id)
            return job.status if job else "missing"

    async def text_of(self, job_id: int) -> str:
        async with self.db.tx() as s:
            job = await s.get(BroadcastJob, job_id)
            return job.text if job else ""

    async def run(self, job_id: int, send: SendFn, rate: float = 10.0,
                  batch: int = 25) -> Dict[str, int]:
        """ارسال دسته‌ای با محدودیت سرعت. توقف‌پذیر و قابل ادامه (فقط pending ارسال می‌شود)."""
        text = await self.text_of(job_id)
        delay = batch / max(rate, 0.1)
        while True:
            if await self.status(job_id) != "running":
                break
            async with self.db.tx() as s:
                rows = (await s.execute(
                    select(BroadcastRecipient, User).join(User, User.id == BroadcastRecipient.user_id)
                    .where(BroadcastRecipient.job_id == job_id,
                           BroadcastRecipient.status == "pending")
                    .order_by(BroadcastRecipient.id).limit(batch))).all()
            if not rows:
                async with self.db.tx() as s:
                    await s.execute(update(BroadcastJob).where(
                        BroadcastJob.id == job_id, BroadcastJob.status == "running")
                        .values(status="done", updated_at=utcnow()))
                break
            results = await asyncio.gather(
                *(self._send_one(send, user, text) for _, user in rows))
            async with self.db.tx() as s:
                for (rec, _), status in zip(rows, results):
                    await s.execute(update(BroadcastRecipient)
                                    .where(BroadcastRecipient.id == rec.id,
                                           BroadcastRecipient.status == "pending")
                                    .values(status=status, updated_at=utcnow()))
            await asyncio.sleep(delay)
        return await self.counts(job_id)

    @staticmethod
    async def _send_one(send: SendFn, user: User, text: str) -> str:
        for attempt in range(3):
            try:
                await send(user, text)
                return "sent"
            except AdapterError as exc:
                if exc.user_blocked_bot:
                    return "blocked"
                if not exc.retryable:
                    return "failed"
                await asyncio.sleep(1.0 * (attempt + 1))
            except Exception:  # noqa: BLE001
                log.exception("broadcast send crashed")
                return "failed"
        return "failed"
