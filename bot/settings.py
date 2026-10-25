from __future__ import annotations

from sqlalchemy import select

from database.models import BotSetting
from database.session import Database
from . import texts

DEFAULTS = {
    "match_timeout": "120",
    "random_chat_enabled": "1",
    "anon_links_enabled": "1",
    "rate_limit_messages": "20",
    "rate_limit_window": "10",
    "welcome_text": texts.START,
}


class SettingsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, key: str) -> str:
        async with self.db.tx() as s:
            row = await s.get(BotSetting, key)
            return row.value if row else DEFAULTS[key]

    async def get_int(self, key: str) -> int:
        try:
            return int(await self.get(key))
        except ValueError:
            return int(DEFAULTS[key])

    async def get_bool(self, key: str) -> bool:
        return (await self.get(key)) == "1"

    async def set(self, key: str, value: str) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        async with self.db.tx() as s:
            row = await s.get(BotSetting, key)
            if row:
                row.value = value
            else:
                s.add(BotSetting(key=key, value=value))

    async def all(self) -> dict:
        async with self.db.tx() as s:
            rows = (await s.execute(select(BotSetting))).scalars().all()
        out = dict(DEFAULTS)
        out.update({r.key: r.value for r in rows})
        return out
