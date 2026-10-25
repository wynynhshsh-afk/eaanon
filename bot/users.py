from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from database.models import Ban, User, utcnow
from database.session import Database


class UserService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_or_create(self, platform_id: str, chat_id: Optional[str] = None) -> User:
        for attempt in (1, 2):
            try:
                async with self.db.tx() as s:
                    user = (await s.execute(
                        select(User).where(User.platform_id == platform_id)
                    )).scalar_one_or_none()
                    if user is None:
                        user = User(platform_id=platform_id, chat_id=chat_id)
                        s.add(user)
                        await s.flush()
                    else:
                        user.last_seen = utcnow()
                        if chat_id and user.chat_id != chat_id:
                            user.chat_id = chat_id
                    return user
            except IntegrityError:
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    async def get(self, user_id: int) -> Optional[User]:
        async with self.db.tx() as s:
            return await s.get(User, user_id)

    async def find(self, query: str) -> Optional[User]:
        async with self.db.tx() as s:
            q = select(User).where(User.platform_id == query)
            if query.isdigit():
                q = select(User).where((User.platform_id == query) | (User.id == int(query)))
            return (await s.execute(q.limit(1))).scalars().first()

    async def chat_id_of(self, user_id: int) -> Optional[str]:
        async with self.db.tx() as s:
            u = await s.get(User, user_id)
            return (u.chat_id or u.platform_id) if u else None

    async def set_state(self, user_id: int, state: str,
                        data: Optional[Dict[str, Any]] = None, **extra: Any) -> None:
        values: Dict[str, Any] = {"state": state, "updated_at": utcnow(),
                                  "state_data": json.dumps(data) if data else None}
        values.update(extra)
        async with self.db.tx() as s:
            await s.execute(update(User).where(User.id == user_id).values(**values))

    @staticmethod
    def data_of(user: User) -> Dict[str, Any]:
        try:
            return json.loads(user.state_data) if user.state_data else {}
        except ValueError:
            return {}

    async def active_ban(self, user_id: int, now: Optional[datetime] = None) -> Optional[Ban]:
        now = now or utcnow()
        async with self.db.tx() as s:
            q = select(Ban).where(
                Ban.user_id == user_id, Ban.revoked.is_(False),
                (Ban.until.is_(None)) | (Ban.until > now),
            ).order_by(Ban.id.desc()).limit(1)
            return (await s.execute(q)).scalars().first()
