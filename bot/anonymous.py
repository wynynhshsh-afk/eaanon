from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional, Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from config import Config
from database.models import (
    AnonymousLink, AnonymousMessage, Block, Report, utcnow,
)
from database.session import Database

PAYLOAD_PREFIX = "a_"


class AnonymousService:
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg

    # --- links ---
    async def get_link(self, user_id: int) -> AnonymousLink:
        async with self.db.tx() as s:
            link = (await s.execute(select(AnonymousLink).where(
                AnonymousLink.user_id == user_id, AnonymousLink.revoked.is_(False)
            ).limit(1))).scalars().first()
            if link is None:
                link = AnonymousLink(user_id=user_id, token=secrets.token_urlsafe(12))
                s.add(link)
                await s.flush()
            return link

    async def regenerate(self, user_id: int) -> AnonymousLink:
        async with self.db.tx() as s:
            await s.execute(update(AnonymousLink).where(
                AnonymousLink.user_id == user_id, AnonymousLink.revoked.is_(False)
            ).values(revoked=True, updated_at=utcnow()))
            link = AnonymousLink(user_id=user_id, token=secrets.token_urlsafe(12))
            s.add(link)
            await s.flush()
            return link

    async def resolve(self, token: str) -> Optional[AnonymousLink]:
        if not token or len(token) > 64:
            return None
        async with self.db.tx() as s:
            return (await s.execute(select(AnonymousLink).where(
                AnonymousLink.token == token, AnonymousLink.revoked.is_(False)
            ))).scalars().first()

    def url(self, token: str, bot_username: str) -> str:
        return self.cfg.link_template.format(
            bot_username=bot_username, payload=PAYLOAD_PREFIX + token)

    # --- blocks ---
    async def is_blocked(self, blocker_id: int, blocked_id: int) -> bool:
        async with self.db.tx() as s:
            return (await s.execute(select(Block.id).where(
                Block.blocker_id == blocker_id, Block.blocked_id == blocked_id
            ).limit(1))).first() is not None

    async def block(self, blocker_id: int, blocked_id: int) -> None:
        try:
            async with self.db.tx() as s:
                exists = (await s.execute(select(Block.id).where(
                    Block.blocker_id == blocker_id, Block.blocked_id == blocked_id
                ))).first()
                if not exists:
                    s.add(Block(blocker_id=blocker_id, blocked_id=blocked_id))
        except IntegrityError:
            pass

    # --- messages ---
    async def create_message(self, sender_id: int, recipient_id: int, text: str,
                             link_id: Optional[int] = None,
                             parent_id: Optional[int] = None) -> AnonymousMessage:
        async with self.db.tx() as s:
            msg = AnonymousMessage(sender_id=sender_id, recipient_id=recipient_id,
                                   text=text, link_id=link_id, parent_id=parent_id)
            s.add(msg)
            await s.flush()
            return msg

    async def set_status(self, msg_id: int, status: str) -> None:
        async with self.db.tx() as s:
            await s.execute(update(AnonymousMessage).where(AnonymousMessage.id == msg_id)
                            .values(status=status, updated_at=utcnow()))

    async def get_for_recipient(self, msg_id: int, user_id: int) -> Optional[AnonymousMessage]:
        async with self.db.tx() as s:
            msg = await s.get(AnonymousMessage, msg_id)
            if msg is None or msg.recipient_id != user_id or msg.status == "deleted":
                return None
            return msg

    async def mark_seen(self, msg_id: int, user_id: int) -> Tuple[Optional[AnonymousMessage], bool]:
        """(پیام, آیا همین الان برای اولین بار دیده شد)."""
        async with self.db.tx() as s:
            msg = await s.get(AnonymousMessage, msg_id)
            if msg is None or msg.recipient_id != user_id or msg.status == "deleted":
                return None, False
            r = await s.execute(update(AnonymousMessage).where(
                AnonymousMessage.id == msg_id,
                AnonymousMessage.status.in_(("received", "delivered")),
            ).values(status="seen", seen_at=utcnow(), updated_at=utcnow()))
            first = bool(r.rowcount)
            await s.refresh(msg)
            return msg, first

    async def inbox_page(self, user_id: int, offset: int) -> Tuple[Optional[AnonymousMessage], int]:
        async with self.db.tx() as s:
            base = (AnonymousMessage.recipient_id == user_id, AnonymousMessage.status != "deleted")
            total = (await s.execute(select(func.count(AnonymousMessage.id)).where(*base))).scalar_one()
            if total == 0:
                return None, 0
            offset = max(0, min(offset, total - 1))
            msg = (await s.execute(select(AnonymousMessage).where(*base)
                                   .order_by(AnonymousMessage.id.desc())
                                   .offset(offset).limit(1))).scalars().first()
            return msg, total

    async def unread_count(self, user_id: int) -> int:
        async with self.db.tx() as s:
            return (await s.execute(select(func.count(AnonymousMessage.id)).where(
                AnonymousMessage.recipient_id == user_id,
                AnonymousMessage.status.in_(("received", "delivered"))))).scalar_one()

    async def delete(self, msg_id: int, user_id: int) -> bool:
        async with self.db.tx() as s:
            r = await s.execute(update(AnonymousMessage).where(
                AnonymousMessage.id == msg_id, AnonymousMessage.recipient_id == user_id,
                AnonymousMessage.status != "deleted",
            ).values(status="deleted", text="", updated_at=utcnow()))
            return bool(r.rowcount)

    async def report(self, reporter_id: int, msg: AnonymousMessage, reason: str) -> None:
        async with self.db.tx() as s:
            s.add(Report(reporter_id=reporter_id, reported_id=msg.sender_id,
                         message_id=msg.id, reason=reason[:200]))

    async def purge_old(self, days: int) -> int:
        cutoff = utcnow() - timedelta(days=days)
        async with self.db.tx() as s:
            used = select(Report.message_id).where(Report.status == "open",
                                                   Report.message_id.is_not(None))
            parents = select(AnonymousMessage.parent_id).where(
                AnonymousMessage.parent_id.is_not(None))
            r = await s.execute(delete(AnonymousMessage).where(
                AnonymousMessage.created_at < cutoff,
                AnonymousMessage.id.notin_(used), AnonymousMessage.id.notin_(parents)))
            return r.rowcount or 0
