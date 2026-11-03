from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import List, Optional, Tuple

from config import Config
from database.models import AnonymousLink, AnonymousMessage, Report, User, iso, utcnow
from database.session import Database, eq, is_unique_violation
from . import entities as E

log = logging.getLogger("anonymous")

PAYLOAD_PREFIX = "a_"
_LIVE = ("received", "delivered")


class AnonymousService:
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg
        # ستون anonymous_messages.entities باید روی دیتابیس واقعی باشه (migrations/001_...sql)؛
        # وگرنه نوشتنش صف write-back کش رو برای همیشه گیر می‌ندازه. probe_entities موقع استارت چک می‌کنه.
        self.entities_enabled = True

    async def probe_entities(self) -> bool:
        try:
            self.entities_enabled = await self.db.has_column("anonymous_messages", "entities")
        except Exception:  # noqa: BLE001
            log.exception("entities column probe failed; formatting storage disabled")
            self.entities_enabled = False
        if not self.entities_enabled:
            log.warning("anonymous_messages.entities missing: run migrations/001_message_entities.sql "
                        "- messages will be delivered without formatting until then")
        return self.entities_enabled

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
                             parent_id: Optional[int] = None,
                             entities: Optional[E.Entities] = None) -> AnonymousMessage:
        row = await self.db.rpc("anon_create_message", {
            "p_sender": sender_id, "p_recipient": recipient_id, "p_text": text,
            "p_link_id": link_id, "p_parent_id": parent_id})
        msg = AnonymousMessage.from_row(row)
        payload = E.dumps(entities or [])
        if payload and self.entities_enabled:
            await self.db.update("anonymous_messages", {"entities": payload}, eq(id=msg.id))
            msg.entities = payload
        return msg

    async def awaiting_notice(self, user_id: int) -> List[int]:
        """پیام‌هایی که هنوز اعلان «پیام جدید» براشون نرفته (status=received)، قدیمی‌ترین اول."""
        rows = await self.db.rows(
            "anonymous_messages", cols="id", order="id",
            where=[("recipient_id", "eq", user_id), ("status", "eq", "received")])
        return [int(r["id"]) for r in rows]

    async def mark_delivered(self, msg_ids: List[int]) -> None:
        if msg_ids:
            await self.db.update("anonymous_messages", {"status": "delivered"},
                                 [("id", "in", msg_ids), ("status", "eq", "received")])

    async def unread(self, user_id: int, limit: int) -> Tuple[List[AnonymousMessage], int]:
        """(قدیمی‌ترین `limit` پیام خوانده‌نشده، تعداد کل خوانده‌نشده‌ها)."""
        where = [("recipient_id", "eq", user_id), ("status", "in", _LIVE)]
        total = await self.db.count("anonymous_messages", where)
        if total == 0:
            return [], 0
        rows = await self.db.rows("anonymous_messages", AnonymousMessage, where=where,
                                  order="id", limit=limit)
        return rows, total

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
        values = {"status": "deleted", "text": ""}
        if self.entities_enabled:
            values["entities"] = None
        rows = await self.db.update(
            "anonymous_messages", values,
            [("id", "eq", msg_id), ("recipient_id", "eq", user_id), ("status", "neq", "deleted")])
        return bool(rows)

    async def report(self, reporter_id: int, msg: AnonymousMessage, reason: str) -> Report:
        return await self.db.insert("reports", {
            "reporter_id": reporter_id, "reported_id": msg.sender_id,
            "message_id": msg.id, "reason": reason[:200]}, Report)

    async def purge_old(self, days: int) -> int:
        cutoff = utcnow() - timedelta(days=days)
        return int(await self.db.rpc("anon_purge_old", {"p_cutoff": iso(cutoff)}) or 0)
