from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Type, TypeVar

from .engine import FakeSupabase
from .session import Database, Filter
from .session import SchemaMissing  # noqa: F401  (re-exported for callers)

log = logging.getLogger("cache")

T = TypeVar("T")

PAGE = 1000

# جدول‌هایی که رام‌کش می‌شن (خوندن از حافظه، نوشتن async به Supabase).
# پاسخ‌گویی لحظه‌ای ربات (state کاربر، صف تطبیق، چت فعال، پیام‌ها، تنظیمات) از اینجا میاد.
CACHE_TABLES = (
    "users", "anonymous_links", "matching_queue", "active_chats", "anonymous_messages",
    "blocks", "reports", "bans", "broadcast_jobs", "broadcast_recipients", "bot_settings",
)
PRIMARY_KEY = {"bot_settings": "key", "matching_queue": "user_id"}

# جدول‌های آرشیوی: فقط نوشته می‌شن، دوباره تو مسیر زنده خونده نمی‌شن.
# کش نمی‌شن (رشد نامحدود حافظه رو جلو می‌گیره)؛ نوشتن‌هاشون هم پس‌زمینه و بدون انتظاره.
ARCHIVE_TABLES = ("message_logs", "admin_audit_logs")

MAX_RETRY_DELAY = 30.0
DRAIN_TIMEOUT = 20.0


class CacheLayer:
    """بین ربات و Supabase می‌شینه: خوندن/نوشتن لحظه‌ای از حافظه، تکرار async به Supabase.

    - جدول‌های CACHE_TABLES موقع استارت از Supabase لود می‌شن (warm) و از اون به بعد خوندن‌ها
      مستقیم از حافظه‌ن؛ نوشتن‌ها اول رو حافظه اعمال می‌شن (ربات فوری جواب درست می‌گیره) و
      همون لحظه یه job برای همون عملیات رو صف پس‌زمینه می‌ره که با همون ترتیب به Supabase برسه.
    - جدول‌های ARCHIVE_TABLES اصلاً کش نمی‌شن؛ نوشتن‌شون پس‌زمینه و بی‌انتظاره، خوندن‌شون
      (فقط تو پنل ادمین/بکاپ که نادره) مستقیم از Supabase میاد.
    - صف پس‌زمینه ترتیب رو حفظ می‌کنه و تا موفقیت retry می‌کنه (drop نمی‌کنه)، که id ها و
      وضعیت کش و Supabase از هم جدا نیفتن. اگه Supabase مدتی قطع باشه، صف تو حافظه جمع می‌شه
      و ربات همچنان سریع کار می‌کنه؛ با وصل شدن دوباره، صف خودش جبران می‌شه.
    """

    def __init__(self, url: str, key: str) -> None:
        self._remote = Database(url, key)
        self._local = Database("", "", client=FakeSupabase(), local=True)
        self._queue: "asyncio.Queue[Optional[Callable[[], Any]]]" = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    # --- چرخه‌ی عمر ---
    async def create_all(self) -> None:
        await self._remote.check()
        self._worker_task = asyncio.create_task(self._worker(), name="supabase-writeback")
        await self.warm()

    async def check(self) -> None:
        await self._remote.check()

    async def warm(self) -> None:
        store: FakeSupabase = self._local.client  # type: ignore[assignment]
        loaded: Dict[str, int] = {}
        for table in CACHE_TABLES:
            pk = PRIMARY_KEY.get(table, "id")
            rows: List[Dict[str, Any]] = []
            offset = 0
            while True:
                page = await self._remote.rows(table, order=pk, limit=PAGE, offset=offset)
                rows.extend(page)
                if len(page) < PAGE:
                    break
                offset += PAGE
            store.tables[table] = rows
            if pk == "id":
                ids = [r["id"] for r in rows if r.get("id") is not None]
                store.seq[table] = max(ids) if ids else 0
            loaded[table] = len(rows)
        log.info("cache warmed from supabase: %s", loaded)

    async def dispose(self) -> None:
        if self._worker_task is not None:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._worker_task, timeout=DRAIN_TIMEOUT)
            except asyncio.TimeoutError:
                log.error("supabase write-back queue did not drain in time; pending writes dropped")
                self._worker_task.cancel()
        await self._remote.dispose()

    # --- صف پس‌زمینه ---
    def _enqueue(self, job: Callable[[], Any]) -> None:
        self._queue.put_nowait(job)

    async def _worker(self) -> None:
        delay = 1.0
        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            while True:
                try:
                    await job()
                    delay = 1.0
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("supabase write-back retry in %.0fs: %s", delay, exc)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, MAX_RETRY_DELAY)
            self._queue.task_done()

    # --- خوندن (همیشه از حافظه برای جدول‌های کش‌شده) ---
    async def rows(self, table: str, model: Optional[Type[T]] = None, **kw: Any) -> List[Any]:
        if table in ARCHIVE_TABLES:
            return await self._remote.rows(table, model, **kw)
        return await self._local.rows(table, model, **kw)

    async def one(self, table: str, model: Optional[Type[T]] = None, **kw: Any) -> Optional[Any]:
        if table in ARCHIVE_TABLES:
            return await self._remote.one(table, model, **kw)
        return await self._local.one(table, model, **kw)

    async def count(self, table: str, where: Sequence[Filter] = ()) -> int:
        if table in ARCHIVE_TABLES:
            return await self._remote.count(table, where)
        return await self._local.count(table, where)

    # --- نوشتن (رو حافظه فوری، به Supabase پس‌زمینه) ---
    async def insert(self, table: str, values: Any, model: Optional[Type[T]] = None) -> Any:
        if table in ARCHIVE_TABLES:
            self._enqueue(lambda: self._remote.insert(table, values))
            return None
        row = await self._local.insert(table, values, model)
        self._enqueue(lambda: self._remote.insert(table, values))
        return row

    async def upsert(self, table: str, values: Any, on_conflict: str,
                     ignore_duplicates: bool = False) -> List[Dict[str, Any]]:
        if table in ARCHIVE_TABLES:
            self._enqueue(lambda: self._remote.upsert(table, values, on_conflict, ignore_duplicates))
            return []
        row = await self._local.upsert(table, values, on_conflict, ignore_duplicates)
        self._enqueue(lambda: self._remote.upsert(table, values, on_conflict, ignore_duplicates))
        return row

    async def update(self, table: str, values: Dict[str, Any],
                     where: Sequence[Filter]) -> List[Dict[str, Any]]:
        if table in ARCHIVE_TABLES:
            self._enqueue(lambda: self._remote.update(table, values, where))
            return []
        row = await self._local.update(table, values, where)
        self._enqueue(lambda: self._remote.update(table, values, where))
        return row

    async def delete(self, table: str, where: Sequence[Filter]) -> List[Dict[str, Any]]:
        if table in ARCHIVE_TABLES:
            self._enqueue(lambda: self._remote.delete(table, where))
            return []
        row = await self._local.delete(table, where)
        self._enqueue(lambda: self._remote.delete(table, where))
        return row

    # --- rpc ---
    async def rpc(self, name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = params or {}
        if name == "anon_create_message":
            # تابع واقعی هم‌زمان anonymous_messages (کش‌شده) و message_logs (آرشیو) رو می‌نویسه.
            # اینجا فقط بخش کش‌شده رو رو حافظه می‌زنیم؛ اجرای واقعی رو (هر دو جدول، اتمیک)
            # پس‌زمینه رو Supabase صدا می‌زنیم که message_logs هیچ‌وقت رو حافظه نمونه.
            rows = await self._local.insert("anonymous_messages", {
                "sender_id": params["p_sender"], "recipient_id": params["p_recipient"],
                "text": params["p_text"], "link_id": params.get("p_link_id"),
                "parent_id": params.get("p_parent_id")})
            self._enqueue(lambda: self._remote.rpc(name, params))
            return rows[0] if rows else None
        result = await self._local.rpc(name, params)
        self._enqueue(lambda: self._remote.rpc(name, params))
        return result
