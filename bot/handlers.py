from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Set

from adapters.base import (
    Adapter, AdapterError, Button, IncomingCallback, IncomingMessage, Keyboard, Update,
)
from config import Config
from database.models import User, utcnow
from database.session import Database
from . import keyboards as K
from . import texts as T
from .admin import AdminPanel, AdminService
from .advertising import Advertising
from .anonymous import PAYLOAD_PREFIX, AnonymousService
from .chat_manager import ChatManager
from .matcher import Matcher
from .ratelimit import RateLimiter
from .settings import SettingsService
from .users import UserService

log = logging.getLogger("bot")

MAX_ANON_LEN = 2000
BLOCKED_EXT = {"exe", "apk", "bat", "cmd", "scr", "msi", "com", "jar", "vbs", "js", "ps1",
               "sh", "dll", "lnk", "apkm", "xapk"}
STATUS_FA = {"received": "دریافت‌شده", "delivered": "تحویل‌داده‌شده", "seen": "دیده‌شده",
             "replied": "پاسخ‌داده‌شده"}


class BotApp:
    def __init__(self, cfg: Config, db: Database, adapter: Adapter) -> None:
        self.cfg = cfg
        self.db = db
        self.adapter = adapter
        self.settings = SettingsService(db)
        self.users = UserService(db)
        self.matcher = Matcher(db, self.settings)
        self.chats = ChatManager(db)
        self.anon = AnonymousService(db, cfg)
        self.admin = AdminService(db)
        self.ads = Advertising(db)
        self.panel = AdminPanel(self)
        self.limiter = RateLimiter()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._bc_tasks: Dict[int, asyncio.Task] = {}

    # ---------- helpers ----------
    def is_owner(self, platform_id: str) -> bool:
        return bool(self.cfg.owner_id) and platform_id == self.cfg.owner_id

    async def say(self, chat_id: str, text: str, kb: Optional[Keyboard] = None) -> bool:
        try:
            await self.adapter.send_message(chat_id, text, kb)
            return True
        except AdapterError as exc:
            log.warning("send failed (code %s)", exc.code)
            return False

    async def notify(self, user_id: int, text: str, kb: Optional[Keyboard] = None) -> bool:
        chat_id = await self.users.chat_id_of(user_id)
        return bool(chat_id) and await self.say(chat_id, text, kb)

    async def menu(self, chat_id: str, text: Optional[str] = None) -> None:
        await self.say(chat_id, text or await self.settings.get("welcome_text"), K.main_menu())

    # ---------- dispatch ----------
    async def handle_update(self, update: Update) -> None:
        lock = self._locks.setdefault(update.user_id, asyncio.Lock())
        async with lock:
            try:
                if isinstance(update, IncomingCallback):
                    await self._on_callback(update)
                elif isinstance(update, IncomingMessage):
                    await self._on_message(update)
            except Exception:  # noqa: BLE001
                log.exception("update handling failed")

    async def _gate(self, user: User, chat_id: str) -> bool:
        """بن و محدودیت سرعت. True یعنی ادامه بده."""
        if not self.is_owner(user.platform_id):
            ban = await self.users.active_ban(user.id)
            if ban:
                until = f"\nتا: {ban.until:%Y-%m-%d %H:%M}" if ban.until else ""
                if self.limiter.allow(f"banmsg:{user.id}", 1, 30):
                    await self.say(chat_id, f"حساب شما مسدود شده.\nدلیل: {ban.reason or '-'}{until}")
                return False
        limit = await self.settings.get_int("rate_limit_messages")
        window = await self.settings.get_int("rate_limit_window")
        if not self.limiter.allow(f"msg:{user.id}", limit, window):
            if self.limiter.allow(f"warn:{user.id}", 1, window):
                await self.say(chat_id, "آروم‌تر! پیام‌هات خیلی سریع می‌رسن.")
            return False
        return True

    # ---------- messages ----------
    async def _on_message(self, m: IncomingMessage) -> None:
        if m.chat_type not in ("private", ""):
            return
        user = await self.users.get_or_create(m.user_id, m.chat_id)
        chat_id = m.chat_id
        if not await self._gate(user, chat_id):
            return
        text = (m.text or "").strip()
        cmd, _, arg = text.partition(" ")
        cmdl = cmd.lower().split("@")[0]

        if cmdl == "/start":
            return await self._cmd_start(user, chat_id, arg.strip())
        if cmdl in ("/myid", "/id", ".شناسه"):
            return await self.say(chat_id, f"شناسهٔ عددی حساب شما: {m.user_id}")
        if cmdl in ("/admin", "/panel", ".پنل") and self.is_owner(user.platform_id):
            return await self.panel.open(user, chat_id)
        if cmdl == "/cancel":
            return await self._cancel(user, chat_id)

        if user.state.startswith("admin_") and self.is_owner(user.platform_id):
            if await self.panel.on_text(user, chat_id, text):
                return
        if user.state == "chatting":
            return await self._relay(user, m)
        if user.state == "composing_anon":
            return await self._anon_send(user, m, text)
        if user.state == "replying":
            return await self._anon_reply(user, m, text)

        # حالت جایگزین متنی: متن دکمه = خود دکمه
        data = K.LABEL_TO_DATA.get(text)
        if data:
            return await self._dispatch_callback(user, chat_id, data, "")
        if user.state == "waiting":
            return await self.say(chat_id, "هنوز دارم دنبال یه ناشناس می‌گردم.", K.searching())
        await self.menu(chat_id)

    async def _cmd_start(self, user: User, chat_id: str, arg: str) -> None:
        if user.state == "chatting":
            return await self.say(chat_id, "الان تو مکالمه‌ای. برای خروج دکمه پایان مکالمه رو بزن.",
                                  K.chat_controls())
        if user.state == "waiting":
            await self.matcher.leave(user.id)
        await self.users.set_state(user.id, "idle")
        if arg.startswith(PAYLOAD_PREFIX):
            return await self._anon_begin(user, chat_id, arg[len(PAYLOAD_PREFIX):])
        await self.menu(chat_id)

    async def _cancel(self, user: User, chat_id: str) -> None:
        if user.state == "chatting":
            return await self.say(chat_id, "برای خروج دکمه پایان مکالمه رو بزن.", K.chat_controls())
        if user.state == "waiting":
            await self.matcher.leave(user.id)
        await self.users.set_state(user.id, "idle")
        await self.menu(chat_id)

    # ---------- callbacks ----------
    async def _on_callback(self, cb: IncomingCallback) -> None:
        user = await self.users.get_or_create(cb.user_id, cb.chat_id)
        await self.adapter.answer_callback(cb.callback_id)
        if not await self._gate(user, cb.chat_id):
            return
        await self._dispatch_callback(user, cb.chat_id, cb.data, cb.callback_id)

    async def _dispatch_callback(self, user: User, chat_id: str, data: str, cb_id: str) -> None:
        user = await self.users.get(user.id) or user  # وضعیت تازه
        kind, _, rest = data.partition(":")
        if kind == "adm":
            if not self.is_owner(user.platform_id):
                return
            return await self.panel.on_callback(user, chat_id, data)
        if kind == "menu":
            if user.state == "chatting":
                return await self.say(chat_id, "الان تو مکالمه‌ای.", K.chat_controls())
            if rest == "home":
                await self.users.set_state(user.id, "idle")
                return await self.menu(chat_id)
            if rest == "random":
                return await self._random_start(user, chat_id)
            if rest == "link":
                return await self._link_show(user, chat_id, False)
            if rest == "inbox":
                return await self._inbox(user, chat_id, 0)
            if rest == "help":
                return await self.say(chat_id, T.HELP, K.to_menu())
        elif kind == "q" and rest == "cancel":
            return await self._cancel(user, chat_id)
        elif kind == "g" and user.state == "choosing_gender" and rest in ("male", "female"):
            await self.users.set_state(user.id, "choosing_want", gender=rest)
            return await self.say(chat_id, T.ASK_WANT, K.gender_want())
        elif kind == "w" and user.state == "choosing_want" and rest in ("male", "female", "any"):
            return await self._random_join(user, chat_id, rest)
        elif kind == "chat" and rest == "end":
            return await self.end_chat(user, chat_id)
        elif kind == "link" and rest == "new":
            return await self._link_show(user, chat_id, True)
        elif kind == "inbox":
            return await self._inbox(user, chat_id, int(rest or 0))
        elif kind == "view":
            return await self._view(user, chat_id, int(rest))
        elif kind == "reply":
            return await self._reply_begin(user, chat_id, int(rest))
        elif kind == "block":
            return await self._block(user, chat_id, int(rest))
        elif kind == "report":
            return await self._report(user, chat_id, int(rest))
        elif kind == "del":
            mid, _, off = rest.partition(":")
            return await self._delete(user, chat_id, int(mid), int(off or 0))
        if cb_id:
            await self.adapter.answer_callback(cb_id, "این گزینه دیگه معتبر نیست.")

    # ---------- random chat ----------
    async def _random_start(self, user: User, chat_id: str) -> None:
        if not await self.settings.get_bool("random_chat_enabled"):
            return await self.say(chat_id, "چت تصادفی فعلاً غیرفعاله.", K.to_menu())
        if user.state == "waiting":
            return await self.say(chat_id, "داری دنبال یه ناشناس می‌گردی.", K.searching())
        await self.users.set_state(user.id, "choosing_gender")
        await self.say(chat_id, T.ASK_GENDER, K.gender_self())

    async def _random_join(self, user: User, chat_id: str, want: str) -> None:
        await self.users.set_state(user.id, "choosing_want", want=want)
        res = await self.matcher.join(user.id)
        if res.status == "matched" and res.partner_id is not None:
            await self.notify(user.id, T.CONNECTED, K.chat_controls())
            await self.notify(res.partner_id, T.CONNECTED, K.chat_controls())
        elif res.status == "queued":
            await self.say(chat_id, T.SEARCHING, K.searching())
        else:
            await self.users.set_state(user.id, "idle")
            await self.menu(chat_id)

    async def end_chat(self, user: User, chat_id: str) -> None:
        partner = await self.chats.end(user.id)
        await self.menu(chat_id, T.ENDED)
        if partner is not None:
            await self.notify(partner, T.ENDED, K.main_menu())

    async def force_end_chat(self, user_id: int) -> None:
        partner = await self.chats.end(user_id)
        await self.notify(user_id, T.ENDED, K.main_menu())
        if partner is not None:
            await self.notify(partner, T.ENDED, K.main_menu())

    async def _relay(self, user: User, m: IncomingMessage) -> None:
        pair = await self.chats.partner_of(user.id)
        if pair is None:
            await self.users.set_state(user.id, "idle")
            return await self.menu(m.chat_id, T.ENDED)
        _, partner_id = pair
        if m.media_type:
            ext = (m.file_name or "").rsplit(".", 1)[-1].lower() if m.file_name else ""
            if not self.cfg.allow_media or ext in BLOCKED_EXT:
                return await self.say(m.chat_id, "این نوع فایل پشتیبانی نمی‌شه.")
            partner_chat = await self.users.chat_id_of(partner_id)
            ok = bool(partner_chat) and await self.adapter.relay_media(
                partner_chat, m.chat_id, m.message_id)
            if not ok:
                await self.say(m.chat_id, "ارسال این نوع پیام فعلاً پشتیبانی نمی‌شه.")
            return
        if not m.text.strip():
            return
        if not await self.notify(partner_id, m.text):
            await self.say(m.chat_id, "پیامت تحویل داده نشد.")

    async def sweep_queue_once(self, now: Optional[datetime] = None) -> int:
        expired = await self.matcher.sweep(now)
        for uid in expired:
            await self.notify(uid, T.NOT_FOUND, K.main_menu())
        return len(expired)

    # ---------- anonymous link ----------
    async def _link_show(self, user: User, chat_id: str, regenerate: bool) -> None:
        if not await self.settings.get_bool("anon_links_enabled"):
            return await self.say(chat_id, "لینک ناشناس فعلاً غیرفعاله.", K.to_menu())
        link = await (self.anon.regenerate(user.id) if regenerate else self.anon.get_link(user.id))
        url = self.anon.url(link.token, self.adapter.bot_username)
        head = "لینک جدیدت ساخته شد (لینک قبلی باطل شد)." if regenerate else "لینک ناشناس اختصاصی تو:"
        await self.say(chat_id, f"{head}\n\n{url}\n\nهر کی از این لینک وارد بشه می‌تونه بدون اسم بهت پیام بده.",
                       K.link_view())

    async def _anon_begin(self, user: User, chat_id: str, token: str) -> None:
        if not await self.settings.get_bool("anon_links_enabled"):
            return await self.menu(chat_id, "لینک ناشناس فعلاً غیرفعاله.")
        link = await self.anon.resolve(token)
        if link is None or await self.users.active_ban(link.user_id):
            return await self.menu(chat_id, "این لینک معتبر نیست یا باطل شده.")
        if link.user_id == user.id:
            return await self.menu(chat_id, "این لینک خودته! بده به بقیه تا بهت پیام بدن.")
        if await self.anon.is_blocked(link.user_id, user.id):
            return await self.menu(chat_id, "فعلاً امکان ارسال پیام به این شخص نیست.")
        await self.users.set_state(user.id, "composing_anon",
                                   {"owner_id": link.user_id, "link_id": link.id})
        await self.say(chat_id, T.ANON_WELCOME, K.cancel_only())

    async def _anon_send(self, user: User, m: IncomingMessage, text: str) -> None:
        chat_id = m.chat_id
        if m.media_type or not text:
            return await self.say(chat_id, "فعلاً فقط پیام متنی پشتیبانی می‌شه.", K.cancel_only())
        if len(text) > MAX_ANON_LEN:
            return await self.say(chat_id, f"پیام خیلی بلنده (حداکثر {MAX_ANON_LEN} کاراکتر).",
                                  K.cancel_only())
        if not self.limiter.allow(f"anon:{user.id}", 5, 60):
            return await self.say(chat_id, "تعداد پیام‌هات زیاده؛ کمی بعد امتحان کن.")
        data = self.users.data_of(user)
        owner_id, link_id = data.get("owner_id"), data.get("link_id")
        if (owner_id is None or await self.anon.is_blocked(owner_id, user.id)
                or await self.users.active_ban(owner_id)
                or not await self.settings.get_bool("anon_links_enabled")):
            await self.users.set_state(user.id, "idle")
            return await self.menu(chat_id, "فعلاً امکان ارسال پیام به این شخص نیست.")
        msg = await self.anon.create_message(user.id, owner_id, text, link_id=link_id)
        await self._deliver(msg.id, owner_id, is_reply=False)
        await self.users.set_state(user.id, "idle")
        await self.menu(chat_id, "پیامت ارسال شد ✅")

    async def _deliver(self, msg_id: int, recipient_id: int, is_reply: bool) -> None:
        head = "یه پاسخ ناشناس به پیامت رسید! 💬" if is_reply else "یه پیام ناشناس جدید داری! 📩"
        if await self.notify(recipient_id, head, K.view_message(msg_id)):
            await self.anon.set_status(msg_id, "delivered")

    # ---------- inbox ----------
    async def _inbox(self, user: User, chat_id: str, offset: int) -> None:
        msg, total = await self.anon.inbox_page(user.id, offset)
        if msg is None:
            return await self.say(chat_id, "صندوق پیام‌هات خالیه.", K.to_menu())
        offset = max(0, min(offset, total - 1))
        await self._show_message(user, chat_id, msg.id, offset, total, True)

    async def _view(self, user: User, chat_id: str, msg_id: int) -> None:
        await self._show_message(user, chat_id, msg_id, 0, 1, False)

    async def _show_message(self, user: User, chat_id: str, msg_id: int, offset: int,
                            total: int, in_inbox: bool) -> None:
        msg, first = await self.anon.mark_seen(msg_id, user.id)
        if msg is None:
            return await self.say(chat_id, "این پیام وجود نداره.", K.to_menu())
        if first:
            await self.notify(msg.sender_id, T.SEEN)
        unread = await self.anon.unread_count(user.id)
        head = f"پیام ناشناس ({offset + 1}/{total})\n" if in_inbox else "پیام ناشناس\n"
        text = (f"{head}وضعیت: {STATUS_FA.get(msg.status, msg.status)}\n\n{msg.text}\n\n"
                f"پیام‌های خوانده‌نشده: {unread}")
        await self.say(chat_id, text, K.message_actions(msg.id, offset, total, in_inbox))

    async def _reply_begin(self, user: User, chat_id: str, msg_id: int) -> None:
        msg = await self.anon.get_for_recipient(msg_id, user.id)
        if msg is None:
            return await self.say(chat_id, "این پیام وجود نداره.", K.to_menu())
        if await self.anon.is_blocked(msg.sender_id, user.id):
            return await self.say(chat_id, "فعلاً امکان پاسخ به این پیام نیست.", K.to_menu())
        await self.users.set_state(user.id, "replying", {"msg_id": msg_id})
        await self.say(chat_id, "پاسخت رو بنویس. فرستنده هویتت رو نمی‌بینه.", K.cancel_only())

    async def _anon_reply(self, user: User, m: IncomingMessage, text: str) -> None:
        chat_id = m.chat_id
        if m.media_type or not text:
            return await self.say(chat_id, "فعلاً فقط پاسخ متنی پشتیبانی می‌شه.", K.cancel_only())
        if len(text) > MAX_ANON_LEN:
            return await self.say(chat_id, f"پیام خیلی بلنده (حداکثر {MAX_ANON_LEN} کاراکتر).",
                                  K.cancel_only())
        if not self.limiter.allow(f"anon:{user.id}", 5, 60):
            return await self.say(chat_id, "تعداد پیام‌هات زیاده؛ کمی بعد امتحان کن.")
        orig = await self.anon.get_for_recipient(self.users.data_of(user).get("msg_id", 0), user.id)
        await self.users.set_state(user.id, "idle")
        if (orig is None or await self.anon.is_blocked(orig.sender_id, user.id)
                or await self.users.active_ban(orig.sender_id)):
            return await self.menu(chat_id, "فعلاً امکان پاسخ به این پیام نیست.")
        reply = await self.anon.create_message(user.id, orig.sender_id, text, parent_id=orig.id)
        await self.anon.set_status(orig.id, "replied")
        await self._deliver(reply.id, orig.sender_id, is_reply=True)
        await self.menu(chat_id, "پاسخت ارسال شد ✅")

    async def _block(self, user: User, chat_id: str, msg_id: int) -> None:
        msg = await self.anon.get_for_recipient(msg_id, user.id)
        if msg is None:
            return await self.say(chat_id, "این پیام وجود نداره.", K.to_menu())
        await self.anon.block(user.id, msg.sender_id)
        await self.say(chat_id, "فرستنده بلاک شد و دیگه نمی‌تونه بهت پیام بده.", K.to_menu())

    async def _report(self, user: User, chat_id: str, msg_id: int) -> None:
        msg = await self.anon.get_for_recipient(msg_id, user.id)
        if msg is None:
            return await self.say(chat_id, "این پیام وجود نداره.", K.to_menu())
        await self.anon.report(user.id, msg, "گزارش از صندوق پیام")
        await self.say(chat_id, "گزارشت ثبت شد. ممنون!", K.to_menu())

    async def _delete(self, user: User, chat_id: str, msg_id: int, offset: int) -> None:
        await self.anon.delete(msg_id, user.id)
        await self._inbox(user, chat_id, offset)

    # ---------- admin helpers ----------
    async def ban_user(self, admin_pid: str, user_id: int, reason: str, hours: Optional[int]) -> None:
        ban = await self.admin.ban(admin_pid, user_id, reason, hours)
        await self.matcher.leave(user_id)
        partner = await self.chats.end(user_id)
        await self.users.set_state(user_id, "idle")
        until = f"\nتا: {ban.until:%Y-%m-%d %H:%M}" if ban.until else ""
        await self.notify(user_id, f"حساب شما مسدود شد.\nدلیل: {reason or '-'}{until}")
        if partner is not None:
            await self.notify(partner, T.ENDED, K.main_menu())

    # ---------- broadcast ----------
    async def _send_ad(self, user: User, text: str) -> None:
        await self.adapter.send_message(user.chat_id or user.platform_id, text)

    async def run_broadcast(self, job_id: int) -> None:
        counts = await self.ads.run(job_id, self._send_ad, self.cfg.broadcast_rate)
        status = await self.ads.status(job_id)
        if self.cfg.owner_id and status in ("done", "cancelled"):
            owner = await self.users.find(self.cfg.owner_id)
            if owner:
                await self.notify(owner.id, (
                    f"گزارش تبلیغ #{job_id} ({'تموم شد' if status == 'done' else 'لغو شد'})\n"
                    f"موفق: {counts['sent']}\nناموفق: {counts['failed']}\n"
                    f"مسدودکننده ربات: {counts['blocked']}\nباقی‌مانده: {counts['pending']}"))

    async def broadcast_tick(self) -> None:
        for job_id in await self.ads.running_jobs():
            task = self._bc_tasks.get(job_id)
            if task is None or task.done():
                self._bc_tasks[job_id] = asyncio.create_task(self.run_broadcast(job_id))
