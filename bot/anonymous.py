from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional, Tuple

from config import Config
from database.models import AnonymousLink, AnonymousMessage, Report, User, iso, utcnow
from database.session import Database, eq, is_unique_violation

PAYLOAD_PREFIX = "a_"
_LIVE = ("received", "delivered")


class AnonymousService:
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg

    # --- links ---
    async def get_link(self, user_id: int) -> AnonymousLink:
        link = await self.db.one("anonymous_links", AnonymousLink,
                                 where=eq(user_id=user_id, revoked=False))
        if link is None:
            link = await self.db.insert("anonymous_links", {
                "user_id": user_id, "token": secrets.token_urlsafe(12)}, AnonymousLink)
        return link

    async def regenerate(self, user_id: int) -> AnonymousLink:
        await self.db.update("anonymous_links", {"revoked": True},
                             eq(user_id=user_id, revoked=False))
        return await self.db.insert("anonymous_links", {
            "user_id": user_id, "token": secrets.token_urlsafe(12)}, AnonymousLink)

    async def resolve(self, token: str) -> Optional[AnonymousLink]:
        """لینک‌های قدیمی توکنی (a_TOKEN)؛ برای سازگاری با لینک‌های منتشرشده نگه داشته شده."""
        if not token or len(token) > 64:
            return None
        return await self.db.one("anonymous_links", AnonymousLink,
                                 where=eq(token=token, revoked=False))

    async def resolve_target(self, payload: str) -> Optional[Tuple[int, Optional[int]]]:
        """(user_id, link_id) صاحب لینک.

        لینک جدید = شناسهٔ عددی پیام‌رسان (`?start=64621354`) که به سرور وابسته نیست.
        لینک قدیمی `a_TOKEN` هم هنوز کار می‌کنه.
        """
        payload = (payload or "").strip()
        if not payload or len(payload) > 64:
            return None
        if payload.isdigit():
            row = await self.db.one("users", where=eq(platform_id=payload), cols="id")
            return (int(row["id"]), None) if row else None
        if payload.startswith(PAYLOAD_PREFIX):
            link = await self.resolve(payload[len(PAYLOAD_PREFIX):])
            return (link.user_id, link.id) if link else None
        return None

    def url(self, platform_id: str, bot_username: str) -> str:
        return self.cfg.link_template.format(bot_username=bot_username, payload=platform_id)

    # --- blocks ---
    async def is_blocked(self, blocker_id: int, blocked_id: int) -> bool:
        return await self.db.one("blocks", where=eq(blocker_id=blocker_id, blocked_id=blocked_id),
                                 cols="id") is not None

    async def block(self, blocker_id: int, blocked_id: int) -> None:
        try:
            await self.db.upsert("blocks", {"blocker_id": blocker_id, "blocked_id": blocked_id},
                                 on_conflict="blocker_id,blocked_id", ignore_duplicates=True)
        except Exception as exc:  # noqa: BLE001
            if not is_unique_violation(exc):
                raise

    # --- messages ---
    async def create_message(self, sender_id: int, recipient_id: int, text: str,
                             link_id: Optional[int] = None,
                             parent_id: Optional[int] = None) -> AnonymousMessage:
        row = await self.db.rpc("anon_create_message", {
            "p_sender": sender_id, "p_recipient": recipient_id, "p_text": text,
            "p_link_id": link_id, "p_parent_id": parent_id})
        return AnonymousMessage.from_row(row)

    async def set_status(self, msg_id: int, status: str) -> None:
        await self.db.update("anonymous_messages", {"status": status}, eq(id=msg_id))

    async def _owned(self, msg_id: int, user_id: int) -> Optional[AnonymousMessage]:
        msg = await self.db.one("anonymous_messages", AnonymousMessage, where=eq(id=msg_id))
        if msg is None or msg.recipient_id != user_id or msg.status == "deleted":
            return None
        return msg

    async def get_for_recipient(self, msg_id: int, user_id: int) -> Optional[AnonymousMessage]:
        return await self._owned(msg_id, user_id)

    async def mark_seen(self, msg_id: int, user_id: int) -> Tuple[Optional[AnonymousMessage], bool]:
        """(پیام, آیا همین الان برای اولین بار دیده شد)."""
        if await self._owned(msg_id, user_id) is None:
            return None, False
        now = iso(utcnow())
        changed = await self.db.update(
            "anonymous_messages", {"status": "seen", "seen_at": now},
            [("id", "eq", msg_id), ("status", "in", _LIVE)])
        msg = await self.db.one("anonymous_messages", AnonymousMessage, where=eq(id=msg_id))
        return msg, bool(changed)

    async def inbox_page(self, user_id: int, offset: int) -> Tuple[Optional[AnonymousMessage], int]:
        base = [("recipient_id", "eq", user_id), ("status", "neq", "deleted")]
        total = await self.db.count("anonymous_messages", base)
        if total == 0:
            return None, 0
        offset = max(0, min(offset, total - 1))
        msg = await self.db.one("anonymous_messages", AnonymousMessage, where=base,
                                order="id", desc=True, offset=offset)
        return msg, total

    async def unread_count(self, user_id: int) -> int:
        return await self.db.count("anonymous_messages",
                                   [("recipient_id", "eq", user_id), ("status", "in", _LIVE)])

    async def delete(self, msg_id: int, user_id: int) -> bool:
        rows = await self.db.update(
            "anonymous_messages", {"status": "deleted", "text": ""},
            [("id", "eq", msg_id), ("recipient_id", "eq", user_id), ("status", "neq", "deleted")])
        return bool(rows)

    async def report(self, reporter_id: int, msg: AnonymousMessage, reason: str) -> Report:
        return await self.db.insert("reports", {
            "reporter_id": reporter_id, "reported_id": msg.sender_id,
            "message_id": msg.id, "reason": reason[:200]}, Report)

    async def purge_old(self, days: int) -> int:
        cutoff = utcnow() - timedelta(days=days)
        return int(await self.db.rpc("anon_purge_old", {"p_cutoff": iso(cutoff)}) or 0)
