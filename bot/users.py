from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from database.models import Ban, User, iso, utcnow
from database.session import Database, eq, is_unique_violation


def full_name(u: Optional[User]) -> str:
    if u is None:
        return "نامشخص"
    return " ".join(x for x in (u.first_name, u.last_name) if x).strip() or "بدون نام"


def at_username(u: Optional[User]) -> str:
    return f"@{u.username}" if u is not None and u.username else "ندارد"


class UserService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_or_create(self, platform_id: str, chat_id: Optional[str] = None,
                            first_name: str = "", last_name: str = "",
                            username: str = "") -> User:
        first_name, last_name, username = first_name[:128], last_name[:128], username[:128]
        for attempt in (1, 2):
            user = await self.db.one("users", User, where=eq(platform_id=platform_id))
            if user is None:
                try:
                    created = await self.db.insert("users", {
                        "platform_id": platform_id, "chat_id": chat_id,
                        "first_name": first_name or None, "last_name": last_name or None,
                        "username": username or None}, User)
                    if created is not None:
                        return created
                except Exception as exc:  # noqa: BLE001
                    if not is_unique_violation(exc) or attempt == 2:
                        raise
                    continue  # همزمان ساخته شد؛ دوباره می‌خونیم
            values = {"last_seen": iso(utcnow())}
            if chat_id and user.chat_id != chat_id:
                values["chat_id"] = chat_id
            # نام و یوزرنیم هر بار با آخرین مقدار پیام‌رسان هماهنگ می‌شه
            if first_name or last_name or username:
                values.update(first_name=first_name or None, last_name=last_name or None,
                              username=username or None)
            rows = await self.db.update("users", values, eq(id=user.id))
            return User.from_row(rows[0]) if rows else user
        raise RuntimeError("unreachable")

    async def get(self, user_id: int) -> Optional[User]:
        return await self.db.one("users", User, where=eq(id=user_id))

    async def by_platform(self, platform_id: str) -> Optional[User]:
        return await self.db.one("users", User, where=eq(platform_id=str(platform_id)))

    async def find(self, query: str) -> Optional[User]:
        found = await self.db.one("users", User, where=eq(platform_id=query))
        if found is None and query.isdigit() and int(query) < 2 ** 63:
            found = await self.db.one("users", User, where=eq(id=int(query)))
        return found

    async def chat_id_of(self, user_id: int) -> Optional[str]:
        u = await self.get(user_id)
        return (u.chat_id or u.platform_id) if u else None

    async def set_state(self, user_id: int, state: str,
                        data: Optional[Dict[str, Any]] = None, **extra: Any) -> None:
        values: Dict[str, Any] = {"state": state,
                                  "state_data": json.dumps(data) if data else None}
        values.update(extra)
        await self.db.update("users", values, eq(id=user_id))

    @staticmethod
    def data_of(user: User) -> Dict[str, Any]:
        try:
            return json.loads(user.state_data) if user.state_data else {}
        except ValueError:
            return {}

    async def active_ban(self, user_id: int, now: Optional[datetime] = None) -> Optional[Ban]:
        now = now or utcnow()
        return await self.db.one(
            "bans", Ban, order="id", desc=True,
            where=[("user_id", "eq", user_id), ("revoked", "eq", False),
                   ("", "or", f"until.is.null,until.gt.{iso(now)}")])
