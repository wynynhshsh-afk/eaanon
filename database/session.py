from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type, TypeVar

from postgrest.exceptions import APIError
from supabase import ClientOptions, create_client

from .models import Row, iso, utcnow

log = logging.getLogger("db")

DB_TIMEOUT_SECONDS = 10
T = TypeVar("T", bound=Row)

# (ستون، عملگر، مقدار)؛ عملگرها: eq neq in is gt gte lt lte. ("", "or", "a.is.null,b.gt.x") برای OR خام
Filter = Tuple[str, str, Any]


class SchemaMissing(RuntimeError):
    pass


def eq(**kw: Any) -> List[Filter]:
    return [(k, "eq", v) for k, v in kw.items()]


class Database:
    """لایهٔ نازک روی supabase-py. فقط به SUPABASE_URL و SUPABASE_KEY نیاز داره.

    کلاینت supabase-py سینک‌ه؛ هر تماس با asyncio.to_thread اجرا می‌شه تا event loop قفل نشه.
    عملیات اتمیک (تطبیق چت، پایان مکالمه، پاک‌سازی و ...) توابع SQL در supabase_schema.sql هستن
    که با rpc صدا زده می‌شن.
    """

    def __init__(self, url: str, key: str, client: Any = None) -> None:
        if client is None:
            if not url or not key:
                raise SystemExit("SUPABASE_URL and SUPABASE_KEY must be set (see .env.example).")
            client = create_client(url, key, options=ClientOptions(
                postgrest_client_timeout=DB_TIMEOUT_SECONDS))
        self.client = client

    # --- پایه ---
    async def _run(self, fn):
        return await asyncio.to_thread(fn)

    @staticmethod
    def _apply(q: Any, filters: Iterable[Filter]) -> Any:
        for col, op, val in filters:
            if op == "eq":
                q = q.eq(col, val)
            elif op == "neq":
                q = q.neq(col, val)
            elif op == "in":
                q = q.in_(col, list(val))
            elif op == "is":
                q = q.is_(col, val)
            elif op == "gt":
                q = q.gt(col, val)
            elif op == "gte":
                q = q.gte(col, val)
            elif op == "lt":
                q = q.lt(col, val)
            elif op == "lte":
                q = q.lte(col, val)
            elif op == "or":
                q = q.or_(val)
            else:
                raise ValueError(f"unknown filter op {op}")
        return q

    async def check(self) -> None:
        """اتصال و وجود جدول‌ها را وقتی ربات بالا میاد بررسی می‌کنه."""
        try:
            await self._run(lambda: self.client.table("users").select("id").limit(1).execute())
        except APIError as exc:
            if getattr(exc, "code", "") in ("PGRST205", "42P01") or "does not exist" in str(exc) \
                    or "schema cache" in str(exc):
                raise SchemaMissing(
                    "جدول‌ها در Supabase ساخته نشدن. فایل supabase_schema.sql رو یک‌بار در "
                    "SQL Editor اجرا کن.") from exc
            raise

    async def create_all(self) -> None:  # نام قدیمی؛ فقط بررسی اتصال
        await self.check()

    async def dispose(self) -> None:
        return None

    # --- CRUD ---
    async def rows(self, table: str, model: Optional[Type[T]] = None, *,
                   where: Sequence[Filter] = (), cols: str = "*", order: Optional[str] = None,
                   desc: bool = False, limit: Optional[int] = None,
                   offset: int = 0) -> List[Any]:
        def fn():
            q = self.client.table(table).select(cols)
            q = self._apply(q, where)
            if order:
                q = q.order(order, desc=desc)
            if limit is not None:
                q = q.range(offset, offset + limit - 1)
            return q.execute().data or []
        data = await self._run(fn)
        return [model.from_row(r) for r in data] if model else data

    async def one(self, table: str, model: Optional[Type[T]] = None, **kw: Any) -> Optional[Any]:
        found = await self.rows(table, model, limit=1, **kw)
        return found[0] if found else None

    async def count(self, table: str, where: Sequence[Filter] = ()) -> int:
        def fn():
            q = self.client.table(table).select("*", count="exact").limit(1)
            return self._apply(q, where).execute().count or 0
        return int(await self._run(fn))

    async def insert(self, table: str, values: Any, model: Optional[Type[T]] = None) -> Any:
        def fn():
            return self.client.table(table).insert(values).execute().data or []
        data = await self._run(fn)
        if model is None:
            return data
        out = [model.from_row(r) for r in data]
        return out if isinstance(values, list) else (out[0] if out else None)

    async def upsert(self, table: str, values: Any, on_conflict: str,
                     ignore_duplicates: bool = False) -> List[Dict[str, Any]]:
        def fn():
            return self.client.table(table).upsert(
                values, on_conflict=on_conflict, ignore_duplicates=ignore_duplicates
            ).execute().data or []
        return await self._run(fn)

    async def update(self, table: str, values: Dict[str, Any],
                     where: Sequence[Filter]) -> List[Dict[str, Any]]:
        """ردیف‌های تغییرکرده را برمی‌گردونه (برای rowcount)."""
        values = {**values}
        values.setdefault("updated_at", iso(utcnow()))

        def fn():
            return self._apply(self.client.table(table).update(values), where).execute().data or []
        return await self._run(fn)

    async def delete(self, table: str, where: Sequence[Filter]) -> List[Dict[str, Any]]:
        def fn():
            return self._apply(self.client.table(table).delete(), where).execute().data or []
        return await self._run(fn)

    async def rpc(self, name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        def fn():
            return self.client.rpc(name, params or {}).execute().data
        return await self._run(fn)


def is_unique_violation(exc: BaseException) -> bool:
    return isinstance(exc, APIError) and (
        getattr(exc, "code", "") == "23505" or "duplicate key" in str(exc))
