from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Sequence, Union


class AdapterError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, retryable: bool = False,
                 description: str = ""):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.description = description

    @property
    def user_blocked_bot(self) -> bool:
        return self.code == 403


@dataclass(frozen=True)
class Button:
    text: str
    data: str


Keyboard = Sequence[Sequence[Button]]


@dataclass
class IncomingMessage:
    chat_id: str
    user_id: str
    message_id: str
    chat_type: str = "private"
    text: str = ""
    media_type: Optional[str] = None
    file_name: Optional[str] = None
    first_name: str = ""
    last_name: str = ""
    username: str = ""


@dataclass
class IncomingCallback:
    callback_id: str
    chat_id: str
    user_id: str
    message_id: str
    data: str
    first_name: str = ""
    last_name: str = ""
    username: str = ""


Update = Union[IncomingMessage, IncomingCallback]


class Adapter(ABC):
    bot_username: str = ""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    @abstractmethod
    async def get_updates(self) -> List[Update]: ...

    @abstractmethod
    async def send_message(
        self, chat_id: str, text: str, keyboard: Optional[Keyboard] = None
    ) -> Optional[str]: ...

    async def answer_callback(self, callback_id: str, text: Optional[str] = None) -> None:
        return None

    async def relay_media(self, to_chat_id: str, from_chat_id: str, message_id: str) -> bool:
        return False

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, keyboard: Optional[Keyboard] = None
    ) -> bool:
        """پیام موجود را ویرایش می‌کند. False یعنی ویرایش نشد (فراخوان پیام جدید می‌فرستد)."""
        return False

    async def send_document(self, chat_id: str, filename: str, data: bytes,
                            caption: str = "") -> bool:
        return False
