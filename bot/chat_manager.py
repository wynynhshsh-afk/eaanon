from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy import select, update

from database.models import ActiveChat, User, utcnow
from database.session import Database


class ChatManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def partner_of(self, user_id: int) -> Optional[Tuple[int, int]]:
        """(chat_id, partner_user_id) یا None."""
        async with self.db.tx() as s:
            chat = (await s.execute(
                select(ActiveChat).where(
                    ActiveChat.active.is_(True),
                    (ActiveChat.user_a == user_id) | (ActiveChat.user_b == user_id),
                ).limit(1)
            )).scalars().first()
            if chat is None:
                return None
            return chat.id, (chat.user_b if chat.user_a == user_id else chat.user_a)

    async def end(self, user_id: int) -> Optional[int]:
        """مکالمه را برای هر دو طرف می‌بندد؛ شناسه طرف مقابل را برمی‌گرداند."""
        async with self.db.tx() as s:
            chat = (await s.execute(
                select(ActiveChat).where(
                    ActiveChat.active.is_(True),
                    (ActiveChat.user_a == user_id) | (ActiveChat.user_b == user_id),
                ).limit(1).with_for_update()
            )).scalars().first()
            if chat is None:
                await s.execute(update(User).where(User.id == user_id, User.state == "chatting")
                                .values(state="idle", state_data=None))
                return None
            chat.active = False
            chat.ended_at = utcnow()
            await s.execute(update(User).where(User.id.in_([chat.user_a, chat.user_b]),
                                               User.state == "chatting")
                            .values(state="idle", state_data=None, updated_at=utcnow()))
            return chat.user_b if chat.user_a == user_id else chat.user_a
