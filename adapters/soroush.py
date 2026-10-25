"""Adapter واقعی سروش پلاس.

تأییدشده از نمونه‌کد main.py: آدرس پایه، getMe، getUpdates (GET)،
sendMessage (POST JSON)، قالب پاسخ {ok, result, error_code} و ساختار message.

تأییدنشده (فرض مشابه Bot API تلگرام؛ با مستندات رسمی تطبیق بده):
  reply_markup/inline_keyboard، callback_query، answerCallbackQuery،
  copyMessage، و نام فیلدهای رسانه. در صورت رد شدن توسط سرور، ربات
  به حالت متنی (دکمه‌ها به شکل لیست گزینه‌ها) برمی‌گردد.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from .base import (
    Adapter, AdapterError, Button, IncomingCallback, IncomingMessage, Keyboard,
    Update,
)

log = logging.getLogger("soroush")

MEDIA_KEYS = ("photo", "video", "voice", "audio", "document", "sticker",
              "video_note", "animation")


def field(data: Any, name: str, default: Any = None) -> Any:
    """خواندن فیلد بدون وابستگی به حروف بزرگ/کوچک."""
    if not isinstance(data, dict):
        return default
    wanted = name.lower()
    for key, value in data.items():
        if str(key).lower() == wanted:
            return value
    return default


def _chat_id(value: Any) -> Any:
    s = str(value)
    return int(s) if s.lstrip("-").isdigit() else s


def parse_update(raw: Any) -> Optional[Update]:
    if not isinstance(raw, dict):
        return None
    message = field(raw, "message")
    if isinstance(message, dict):
        chat = field(message, "chat") or {}
        sender = field(message, "from") or {}
        if field(sender, "is_bot") or field(sender, "id") is None:
            return None
        media = next((k for k in MEDIA_KEYS if field(message, k)), None)
        doc = field(message, "document")
        return IncomingMessage(
            chat_id=str(field(chat, "id")),
            user_id=str(field(sender, "id")),
            message_id=str(field(message, "message_id", "")),
            chat_type=str(field(chat, "type", "private")).lower(),
            text=str(field(message, "text") or field(message, "caption") or ""),
            media_type=media,
            file_name=str(field(doc, "file_name", "")) if isinstance(doc, dict) else None,
        )
    cb = field(raw, "callback_query")
    if isinstance(cb, dict):
        sender = field(cb, "from") or {}
        msg = field(cb, "message") or {}
        chat = field(msg, "chat") or {}
        if field(sender, "id") is None:
            return None
        return IncomingCallback(
            callback_id=str(field(cb, "id", "")),
            chat_id=str(field(chat, "id", field(sender, "id"))),
            user_id=str(field(sender, "id")),
            message_id=str(field(msg, "message_id", "")),
            data=str(field(cb, "data", "")),
        )
    return None


def with_labels(text: str, keyboard: Optional[Keyboard]) -> str:
    if not keyboard:
        return text
    labels = [b.text for row in keyboard for b in row]
    return text + "\n\nگزینه‌ها (متن گزینه رو بفرست):\n" + "\n".join(f"• {l}" for l in labels)


class SoroushAdapter(Adapter):
    def __init__(self, token: str, api_base: str = "https://api.splus.ir/bot",
                 poll_timeout: int = 30) -> None:
        self._token = token
        self._base = api_base
        self._poll_timeout = poll_timeout
        self._http: Optional[aiohttp.ClientSession] = None
        self._offset: Optional[int] = None
        self._allowed = ["message", "callback_query"]
        self.inline_ok = True

    async def start(self) -> None:
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._poll_timeout + 20)
        )
        me = await self._call("getMe")
        self.bot_username = str(field(me, "username", "") or "")

    async def close(self) -> None:
        if self._http:
            await self._http.close()

    async def _call(self, method: str, params: Optional[Dict[str, Any]] = None,
                    get: bool = False) -> Any:
        assert self._http is not None
        url = f"{self._base}{quote(self._token, safe=':')}/{method}"
        params = params or {}
        try:
            if get:
                flat = {
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict))
                    else ("true" if v is True else "false" if v is False else str(v))
                    for k, v in params.items()
                }
                ctx = self._http.get(url, params=flat)
            else:
                ctx = self._http.post(url, json=params)
            async with ctx as resp:
                payload = await resp.json(content_type=None)
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise AdapterError(f"network error ({type(exc).__name__})", retryable=True) from None
        except ValueError:
            raise AdapterError("invalid JSON from server", retryable=True) from None
        if field(payload, "ok", False) is not True:
            code = field(payload, "error_code", status)
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = None
            raise AdapterError(f"{method} rejected (code {code})", code=code,
                               retryable=bool(code and code >= 500) or code == 429)
        return field(payload, "result")

    async def get_updates(self) -> List[Update]:
        params: Dict[str, Any] = {"timeout": self._poll_timeout, "limit": 50,
                                  "allowed_updates": self._allowed}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            raw_list = await self._call("getUpdates", params, get=True) or []
        except AdapterError as exc:
            if exc.code == 400 and "callback_query" in self._allowed:
                log.warning("allowed_updates with callback_query rejected; text-only mode")
                self._allowed = ["message"]
                self.inline_ok = False
            raise
        updates: List[Update] = []
        for raw in raw_list:
            uid = field(raw, "update_id")
            if uid is not None:
                self._offset = int(uid) + 1
            parsed = parse_update(raw)
            if parsed:
                updates.append(parsed)
        return updates

    async def send_message(self, chat_id, text, keyboard=None):
        params: Dict[str, Any] = {"chat_id": _chat_id(chat_id), "text": text}
        use_markup = bool(keyboard) and self.inline_ok
        if use_markup:
            params["reply_markup"] = {"inline_keyboard": [
                [{"text": b.text, "callback_data": b.data} for b in row] for row in keyboard
            ]}
        elif keyboard:
            params["text"] = with_labels(text, keyboard)
        last: Optional[AdapterError] = None
        for attempt in range(3):
            try:
                result = await self._call("sendMessage", params)
                mid = field(result, "message_id")
                return str(mid) if mid is not None else None
            except AdapterError as exc:
                last = exc
                if exc.code == 400 and use_markup:
                    log.warning("inline keyboard rejected; falling back to text options")
                    self.inline_ok = False
                    params.pop("reply_markup", None)
                    params["text"] = with_labels(text, keyboard)
                    use_markup = False
                    continue
                if not exc.retryable:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last

    async def answer_callback(self, callback_id, text=None):
        try:
            params: Dict[str, Any] = {"callback_query_id": callback_id}
            if text:
                params["text"] = text
            await self._call("answerCallbackQuery", params)
        except AdapterError:
            pass

    async def relay_media(self, to_chat_id, from_chat_id, message_id):
        try:
            await self._call("copyMessage", {
                "chat_id": _chat_id(to_chat_id),
                "from_chat_id": _chat_id(from_chat_id),
                "message_id": _chat_id(message_id),
            })
            return True
        except AdapterError as exc:
            log.info("relay_media failed (code %s)", exc.code)
            return False
