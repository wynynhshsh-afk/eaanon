import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.base import IncomingCallback, IncomingMessage  # noqa: E402
from adapters.fake import FakeAdapter  # noqa: E402
from bot.handlers import BotApp  # noqa: E402
from config import Config  # noqa: E402
from database.session import Database  # noqa: E402
from tests.fake_supabase import FakeSupabase  # noqa: E402

OWNER = "9000"


def make_cfg() -> Config:
    return Config(bot_token="x", owner_id=OWNER, supabase_url="", supabase_key="", api_base="",
                  link_template="https://splus.ir/{bot_username}?start={payload}",
                  poll_timeout=1, broadcast_rate=1000.0, allow_media=True,
                  message_retention_days=90, log_level="WARNING")


class Harness:
    def __init__(self, app: BotApp, fake: FakeAdapter, db: Database, store: FakeSupabase):
        self.app, self.fake, self.db, self.store = app, fake, db, store
        self._cb = 0

    async def say(self, uid, text, **kw):
        await self.app.handle_update(IncomingMessage(
            chat_id=str(uid), user_id=str(uid), message_id="1", text=text, **kw))

    async def click(self, uid, data):
        self._cb += 1
        await self.app.handle_update(IncomingCallback(
            callback_id=str(self._cb), chat_id=str(uid), user_id=str(uid),
            message_id="1", data=data))

    async def connect(self, uid, gender, want):
        await self.click(uid, "menu:random")
        await self.click(uid, f"g:{gender}")
        await self.click(uid, f"w:{want}")

    async def restart(self) -> "Harness":
        db = Database("", "", client=self.store)  # همان دیتابیس، پردازش تازه
        fake = FakeAdapter()
        return Harness(BotApp(make_cfg(), db, fake), fake, db, self.store)


@pytest.fixture
async def h():
    store = FakeSupabase()
    db = Database("", "", client=store)
    await db.create_all()
    fake = FakeAdapter()
    harness = Harness(BotApp(make_cfg(), db, fake), fake, db, store)
    yield harness
    await db.dispose()
