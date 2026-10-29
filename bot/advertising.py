from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, List

from adapters.base import AdapterError
from database.models import BroadcastJob, BroadcastRecipient, User
from database.session import Database, eq

log = logging.getLogger("ads")

SendFn = Callable[[User, str], Awaitable[None]]


class Advertising:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_draft(self, admin_id: str, text: str) -> int:
        job = await self.db.insert("broadcast_jobs", {
            "created_by": admin_id, "text": text, "status": "draft"}, BroadcastJob)
        return job.id

    async def audience_size(self) -> int:
        return int(await self.db.rpc("anon_audience_size") or 0)

    async def start(self, job_id: int) -> bool:
        """گیرندگان را یک‌بار می‌سازد (unique) و کار را running می‌کند."""
        return bool(await self.db.rpc("anon_broadcast_start", {"p_job_id": job_id}))

    async def cancel(self, job_id: int) -> None:
        await self.db.update("broadcast_jobs", {"status": "cancelled"},
                             [("id", "eq", job_id), ("status", "in", ("draft", "running"))])

    async def running_jobs(self) -> List[int]:
        rows = await self.db.rows("broadcast_jobs", where=eq(status="running"), cols="id")
        return [int(r["id"]) for r in rows]

    async def counts(self, job_id: int) -> Dict[str, int]:
        out = {"pending": 0, "sent": 0, "failed": 0, "blocked": 0}
        for name in out:
            out[name] = await self.db.count(
                "broadcast_recipients", eq(job_id=job_id, status=name))
        return out

    async def status(self, job_id: int) -> str:
        job = await self.db.one("broadcast_jobs", BroadcastJob, where=eq(id=job_id))
        return job.status if job else "missing"

    async def text_of(self, job_id: int) -> str:
        job = await self.db.one("broadcast_jobs", BroadcastJob, where=eq(id=job_id))
        return job.text if job else ""

    async def run(self, job_id: int, send: SendFn, rate: float = 10.0,
                  batch: int = 25) -> Dict[str, int]:
        """ارسال دسته‌ای با محدودیت سرعت. توقف‌پذیر و قابل ادامه (فقط pending ارسال می‌شود)."""
        text = await self.text_of(job_id)
        delay = batch / max(rate, 0.1)
        while True:
            if await self.status(job_id) != "running":
                break
            recs = await self.db.rows(
                "broadcast_recipients", BroadcastRecipient, order="id", limit=batch,
                where=eq(job_id=job_id, status="pending"))
            if not recs:
                await self.db.update("broadcast_jobs", {"status": "done"},
                                     eq(id=job_id, status="running"))
                break
            users = await self.db.rows("users", User,
                                       where=[("id", "in", [r.user_id for r in recs])])
            by_id = {u.id: u for u in users}
            targets = [(r, by_id[r.user_id]) for r in recs if r.user_id in by_id]
            results = await asyncio.gather(
                *(self._send_one(send, user, text) for _, user in targets))
            for (rec, _), status in zip(targets, results):
                await self.db.update("broadcast_recipients", {"status": status},
                                     eq(id=rec.id, status="pending"))
            for rec in recs:  # کاربری که حذف شده؛ از صف pending خارج بشه
                if rec.user_id not in by_id:
                    await self.db.update("broadcast_recipients", {"status": "failed"},
                                         eq(id=rec.id, status="pending"))
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
