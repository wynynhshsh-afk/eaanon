from __future__ import annotations

from database.models import BotSetting, iso, utcnow
from database.session import Database, eq
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
        row = await self.db.one("bot_settings", BotSetting, where=eq(key=key))
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
        await self.db.upsert("bot_settings",
                             {"key": key, "value": value, "updated_at": iso(utcnow())},
                             on_conflict="key")

    async def all(self) -> dict:
        rows = await self.db.rows("bot_settings", BotSetting)
        out = dict(DEFAULTS)
        out.update({r.key: r.value for r in rows})
        return out
