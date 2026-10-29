from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type, TypeVar

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _dt(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class Row:
    """پایهٔ ردیف‌ها؛ ستون‌های datetime موقع ساخت از رشتهٔ ISO پارس می‌شن."""
    DATETIMES: tuple = ()

    @classmethod
    def from_row(cls: Type[T], row: Dict[str, Any]) -> T:
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        data = {k: v for k, v in row.items() if k in names}
        for name in cls.DATETIMES:  # type: ignore[attr-defined]
            if name in data:
                data[name] = _dt(data[name])
        return cls(**data)  # type: ignore[call-arg]


@dataclass
class User(Row):
    DATETIMES = ("last_seen", "created_at", "updated_at")
    id: int = 0
    platform_id: str = ""
    chat_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    gender: Optional[str] = None
    want: Optional[str] = None
    state: str = "idle"
    state_data: Optional[str] = None
    last_seen: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class AnonymousLink(Row):
    DATETIMES = ("created_at", "updated_at")
    id: int = 0
    user_id: int = 0
    token: str = ""
    revoked: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class QueueEntry(Row):
    DATETIMES = ("enqueued_at", "expires_at", "created_at", "updated_at")
    user_id: int = 0
    gender: str = ""
    want: str = ""
    enqueued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class ActiveChat(Row):
    DATETIMES = ("started_at", "ended_at", "created_at", "updated_at")
    id: int = 0
    user_a: int = 0
    user_b: int = 0
    active: bool = True
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class AnonymousMessage(Row):
    DATETIMES = ("seen_at", "created_at", "updated_at")
    id: int = 0
    recipient_id: int = 0
    sender_id: int = 0
    link_id: Optional[int] = None
    parent_id: Optional[int] = None
    text: str = ""
    status: str = "received"
    seen_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Block(Row):
    DATETIMES = ("created_at", "updated_at")
    id: int = 0
    blocker_id: int = 0
    blocked_id: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Report(Row):
    DATETIMES = ("created_at", "updated_at")
    id: int = 0
    reporter_id: int = 0
    reported_id: int = 0
    message_id: Optional[int] = None
    reason: str = ""
    status: str = "open"
    resolved_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Ban(Row):
    DATETIMES = ("until", "created_at", "updated_at")
    id: int = 0
    user_id: int = 0
    reason: str = ""
    until: Optional[datetime] = None
    revoked: bool = False
    created_by: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class BroadcastJob(Row):
    DATETIMES = ("created_at", "updated_at")
    id: int = 0
    created_by: str = ""
    text: str = ""
    status: str = "draft"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class BroadcastRecipient(Row):
    DATETIMES = ("created_at", "updated_at")
    id: int = 0
    job_id: int = 0
    user_id: int = 0
    status: str = "pending"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class BotSetting(Row):
    DATETIMES = ("updated_at",)
    key: str = ""
    value: str = ""
    updated_at: Optional[datetime] = None


@dataclass
class MessageLog(Row):
    DATETIMES = ("created_at",)
    id: int = 0
    message_id: Optional[int] = None
    kind: str = "anon"
    sender_id: int = 0
    sender_platform_id: str = ""
    sender_name: str = ""
    sender_username: str = ""
    recipient_id: int = 0
    recipient_platform_id: str = ""
    recipient_name: str = ""
    recipient_username: str = ""
    text: str = ""
    created_at: Optional[datetime] = None
