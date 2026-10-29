from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .base import (
    Adapter, AdapterError, IncomingCallback, IncomingMessage, Keyboard, Update,
)


class FakeAdapter(Adapter):
    """Adapter آزمایشی: همه‌چیز در حافظه."""

    def __init__(self) -> None:
        self.bot_username = "testbot"
        self.sent: List[Tuple[str, str, Optional[Keyboard]]] = []
        self.pending: List[Update] = []
        self.fail_for: Set[str] = set()
        self.media_ok = True
        self.relayed: List[Tuple[str, str, str]] = []
        self.answered: List[str] = []
        self.edits: List[Tuple[str, str, str, Optional[Keyboard]]] = []
        self.documents: List[Tuple[str, str, bytes, str]] = []
        self.edit_ok = True
        self.doc_ok = True
        self._mid = 0

    async def get_updates(self) -> List[Update]:
        out, self.pending = self.pending, []
        return out

    async def send_message(self, chat_id, text, keyboard=None):
        if str(chat_id) in self.fail_for:
            raise AdapterError("forbidden", code=403)
        self._mid += 1
        self.sent.append((str(chat_id), text, keyboard))
        return str(self._mid)

    async def answer_callback(self, callback_id, text=None):
        self.answered.append(callback_id)

    async def relay_media(self, to_chat_id, from_chat_id, message_id):
        if str(to_chat_id) in self.fail_for or not self.media_ok:
            return False
        self.relayed.append((str(to_chat_id), str(from_chat_id), str(message_id)))
        return True

    async def edit_message(self, chat_id, message_id, text, keyboard=None):
        if not self.edit_ok or str(chat_id) in self.fail_for:
            return False
        self.edits.append((str(chat_id), str(message_id), text, keyboard))
        self.sent.append((str(chat_id), text, keyboard))  # آخرین وضعیت صفحه
        return True

    async def send_document(self, chat_id, filename, data, caption=""):
        if not self.doc_ok:
            return False
        self.documents.append((str(chat_id), filename, data, caption))
        return True

    # --- helpers for tests ---
    @property
    def new_sends(self) -> int:
        """تعداد پیام‌های واقعاً جدید (بدون ویرایش‌ها)."""
        return len(self.sent) - len(self.edits)

    def texts_to(self, chat_id: str) -> List[str]:
        return [t for c, t, _ in self.sent if c == str(chat_id)]

    def last_to(self, chat_id: str) -> Optional[Tuple[str, Optional[Keyboard]]]:
        for c, t, k in reversed(self.sent):
            if c == str(chat_id):
                return t, k
        return None

    def buttons_to(self, chat_id: str) -> List[str]:
        last = self.last_to(chat_id)
        if not last or not last[1]:
            return []
        return [b.data for row in last[1] for b in row]
