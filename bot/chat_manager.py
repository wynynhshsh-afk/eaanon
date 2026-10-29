from __future__ import annotations

from typing import Optional, Tuple

from database.models import ActiveChat
from database.session import Database


class ChatManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def partner_of(self, user_id: int) -> Optional[Tuple[int, int]]:
        """(chat_id, partner_user_id) یا None."""
        chat = await self.db.one(
            "active_chats", ActiveChat,
            where=[("active", "eq", True),
                   ("", "or", f"user_a.eq.{user_id},user_b.eq.{user_id}")])
        if chat is None:
            return None
        return chat.id, (chat.user_b if chat.user_a == user_id else chat.user_a)

    async def end(self, user_id: int) -> Optional[int]:
        """مکالمه را برای هر دو طرف می‌بندد؛ شناسه طرف مقابل را برمی‌گرداند."""
        partner = await self.db.rpc("anon_end_chat", {"p_user_id": user_id})
        return int(partner) if partner is not None else None
