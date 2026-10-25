from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

from sqlalchemy import func, select, update

from adapters.base import Button, Keyboard
from database.models import (
    ActiveChat, AdminAuditLog, AnonymousMessage, Ban, BroadcastRecipient, QueueEntry,
    Report, User, utcnow,
)
from database.session import Database

if TYPE_CHECKING:  # pragma: no cover
    from .handlers import BotApp

log = logging.getLogger("admin")


class AdminService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def audit(self, admin_id: str, action: str, target: str = "", detail: str = "") -> None:
        async with self.db.tx() as s:
            s.add(AdminAuditLog(admin_id=admin_id, action=action, target=target, detail=detail[:500]))

    async def stats(self) -> dict:
        now = utcnow()
        async with self.db.tx() as s:
            async def count(q):
                return (await s.execute(q)).scalar_one()
            banned = select(func.count(func.distinct(Ban.user_id))).where(
                Ban.revoked.is_(False), (Ban.until.is_(None)) | (Ban.until > now))
            return {
                "users": await count(select(func.count(User.id))),
                "active": await count(select(func.count(User.id)).where(
                    User.last_seen >= now - timedelta(days=7))),
                "banned": await count(banned),
                "chats": await count(select(func.count(ActiveChat.id)).where(ActiveChat.active.is_(True))),
                "waiting": await count(select(func.count(QueueEntry.user_id))),
                "anon": await count(select(func.count(AnonymousMessage.id))),
                "bc_sent": await count(select(func.count(BroadcastRecipient.id)).where(
                    BroadcastRecipient.status == "sent")),
                "bc_failed": await count(select(func.count(BroadcastRecipient.id)).where(
                    BroadcastRecipient.status.in_(("failed", "blocked")))),
                "reports": await count(select(func.count(Report.id)).where(Report.status == "open")),
            }

    async def ban(self, admin_id: str, user_id: int, reason: str, hours: Optional[int]) -> Ban:
        until = utcnow() + timedelta(hours=hours) if hours else None
        async with self.db.tx() as s:
            ban = Ban(user_id=user_id, reason=reason[:300], until=until, created_by=admin_id)
            s.add(ban)
            await s.flush()
        await self.audit(admin_id, "ban", str(user_id), f"{reason} / {hours or 'permanent'}h")
        return ban

    async def unban(self, admin_id: str, user_id: int) -> int:
        async with self.db.tx() as s:
            r = await s.execute(update(Ban).where(Ban.user_id == user_id, Ban.revoked.is_(False))
                                .values(revoked=True, updated_at=utcnow()))
        await self.audit(admin_id, "unban", str(user_id))
        return r.rowcount or 0

    async def open_reports(self, limit: int = 5):
        async with self.db.tx() as s:
            return (await s.execute(select(Report).where(Report.status == "open")
                                    .order_by(Report.id).limit(limit))).scalars().all()

    async def get_report(self, rid: int):
        async with self.db.tx() as s:
            rep = await s.get(Report, rid)
            msg = await s.get(AnonymousMessage, rep.message_id) if rep and rep.message_id else None
            return rep, msg

    async def close_report(self, admin_id: str, rid: int, action: str) -> None:
        async with self.db.tx() as s:
            await s.execute(update(Report).where(Report.id == rid).values(
                status="closed", resolved_by=admin_id, updated_at=utcnow()))
        await self.audit(admin_id, "report_" + action, str(rid))


class AdminPanel:
    """رابط کاربری پنل مالک؛ فقط بر اساس OWNER_ID عددی."""

    EDITABLE = {"match_timeout": (10, 600), "rate_limit_messages": (3, 200),
                "rate_limit_window": (1, 300)}
    TOGGLES = {"random_chat_enabled": "چت تصادفی", "anon_links_enabled": "لینک ناشناس"}

    def __init__(self, app: "BotApp") -> None:
        self.app = app

    def home_kb(self) -> Keyboard:
        return [[Button("آمار", "adm:stats"), Button("کاربران", "adm:users")],
                [Button("تبلیغات", "adm:bc"), Button("گزارش‌ها", "adm:rep")],
                [Button("تنظیمات", "adm:set")], [Button("خروج از پنل", "menu:home")]]

    async def open(self, user: User, chat_id: str) -> None:
        await self.app.users.set_state(user.id, "idle")
        await self.app.say(chat_id, "پنل مالک", self.home_kb())

    def _back(self) -> Keyboard:
        return [[Button("بازگشت به پنل", "adm:home")]]

    async def on_callback(self, user: User, chat_id: str, data: str) -> None:
        app = self.app
        parts = data.split(":")
        act = parts[1] if len(parts) > 1 else ""
        pid = user.platform_id
        if act == "home":
            return await self.open(user, chat_id)
        if act == "stats":
            st = await app.admin.stats()
            text = (f"کاربران: {st['users']}\nفعال (۷ روز): {st['active']}\nبن‌شده: {st['banned']}\n"
                    f"مکالمه فعال: {st['chats']}\nمنتظر اتصال: {st['waiting']}\n"
                    f"پیام ناشناس: {st['anon']}\nتبلیغ ارسال‌شده: {st['bc_sent']}\n"
                    f"تبلیغ ناموفق/مسدود: {st['bc_failed']}\nگزارش باز: {st['reports']}")
            return await app.say(chat_id, text, self._back())
        if act == "users":
            await app.users.set_state(user.id, "admin_find")
            return await app.say(chat_id, "شناسه داخلی یا شناسه پیام‌رسان کاربر رو بفرست.", self._back())
        if act == "u":
            return await self._show_user(chat_id, int(parts[2]))
        if act == "ban":
            await app.users.set_state(user.id, "admin_ban_reason",
                                      {"uid": int(parts[2]), "hours": int(parts[3])})
            return await app.say(chat_id, "دلیل بن رو بنویس (برای دلیل پیش‌فرض یک خط تیره بفرست).",
                                 self._back())
        if act == "unban":
            n = await app.admin.unban(pid, int(parts[2]))
            return await app.say(chat_id, f"رفع بن انجام شد ({n}).", self._back())
        if act == "kick":
            uid = int(parts[2])
            await app.force_end_chat(uid)
            await app.admin.audit(pid, "force_end_chat", str(uid))
            return await app.say(chat_id, "کاربر از مکالمه خارج شد.", self._back())
        if act == "dq":
            uid = int(parts[2])
            await app.matcher.leave(uid)
            await app.admin.audit(pid, "dequeue", str(uid))
            return await app.say(chat_id, "کاربر از صف حذف شد.", self._back())
        if act == "bc":
            await app.users.set_state(user.id, "admin_bc")
            n = await app.ads.audience_size()
            return await app.say(chat_id, f"متن تبلیغ رو بفرست. مخاطبان فعلی: {n} نفر.", self._back())
        if act == "bcok":
            job = int(parts[2])
            if await app.ads.start(job):
                await app.admin.audit(pid, "broadcast_start", str(job))
                return await app.say(chat_id, "ارسال شروع شد. وقتی تموم بشه گزارش می‌دم.", self._back())
            return await app.say(chat_id, "این تبلیغ دیگه قابل شروع نیست.", self._back())
        if act == "bcno":
            await app.ads.cancel(int(parts[2]))
            await app.admin.audit(pid, "broadcast_cancel", parts[2])
            return await app.say(chat_id, "تبلیغ لغو شد.", self._back())
        if act == "rep":
            return await self._list_reports(chat_id)
        if act == "rv":
            return await self._show_report(chat_id, int(parts[2]))
        if act == "rban":
            rep, _ = await app.admin.get_report(int(parts[2]))
            if rep:
                await app.ban_user(pid, rep.reported_id, "گزارش تخلف", None)
                await app.admin.close_report(pid, rep.id, "ban")
            return await app.say(chat_id, "کاربر بن شد و گزارش بسته شد.", self._back())
        if act == "rdis":
            await app.admin.close_report(pid, int(parts[2]), "dismiss")
            return await app.say(chat_id, "گزارش بسته شد.", self._back())
        if act == "set":
            return await self._show_settings(chat_id)
        if act == "tg":
            key = parts[2]
            if key in self.TOGGLES:
                new = "0" if await app.settings.get_bool(key) else "1"
                await app.settings.set(key, new)
                await app.admin.audit(pid, "toggle", key, new)
            return await self._show_settings(chat_id)
        if act == "ed":
            key = parts[2]
            await app.users.set_state(user.id, "admin_edit", {"key": key})
            hint = (f"مقدار جدید ({self.EDITABLE[key][0]} تا {self.EDITABLE[key][1]}) رو بفرست."
                    if key in self.EDITABLE else "متن جدید رو بفرست (حداکثر ۵۰۰ کاراکتر).")
            return await app.say(chat_id, hint, self._back())

    async def on_text(self, user: User, chat_id: str, text: str) -> bool:
        """True یعنی پیام در همین پنل مصرف شد."""
        app = self.app
        data = app.users.data_of(user)
        pid = user.platform_id
        if user.state == "admin_find":
            target = await app.users.find(text.strip())
            if target is None:
                await app.say(chat_id, "کاربری پیدا نشد.", self._back())
            else:
                await app.users.set_state(user.id, "idle")
                await self._show_user(chat_id, target.id)
            return True
        if user.state == "admin_ban_reason":
            reason = "تخلف" if text.strip() == "-" else text.strip()
            await app.users.set_state(user.id, "idle")
            hours = data["hours"] or None
            await app.ban_user(pid, data["uid"], reason, hours)
            await app.say(chat_id, "کاربر بن شد.", self._back())
            return True
        if user.state == "admin_bc":
            if not text.strip() or len(text) > 3500:
                await app.say(chat_id, "متن نامعتبره.", self._back())
                return True
            job = await app.ads.create_draft(pid, text)
            await app.users.set_state(user.id, "idle")
            n = await app.ads.audience_size()
            await app.say(chat_id, f"پیش‌نمایش تبلیغ:\n\n{text}\n\nمخاطبان: {n} نفر",
                          [[Button("تأیید و ارسال", f"adm:bcok:{job}"),
                            Button("لغو", f"adm:bcno:{job}")]])
            return True
        if user.state == "admin_edit":
            key = data.get("key", "")
            await app.users.set_state(user.id, "idle")
            if key in self.EDITABLE:
                lo, hi = self.EDITABLE[key]
                if not text.strip().isdigit() or not lo <= int(text) <= hi:
                    await app.say(chat_id, f"مقدار باید عددی بین {lo} و {hi} باشه.", self._back())
                    return True
                await app.settings.set(key, text.strip())
            elif key == "welcome_text":
                if not text.strip() or len(text) > 500:
                    await app.say(chat_id, "متن نامعتبره.", self._back())
                    return True
                await app.settings.set(key, text.strip())
            else:
                return True
            await app.admin.audit(pid, "set_setting", key, text[:100])
            await self._show_settings(chat_id)
            return True
        return False

    async def _show_user(self, chat_id: str, uid: int) -> None:
        app = self.app
        u = await app.users.get(uid)
        if u is None:
            return await app.say(chat_id, "کاربر پیدا نشد.", self._back())
        ban = await app.users.active_ban(uid)
        ban_txt = "ندارد"
        if ban:
            ban_txt = f"دارد؛ دلیل: {ban.reason or '-'}؛ تا: {ban.until or 'دائمی'}"
        text = (f"شناسه داخلی: {u.id}\nشناسه پیام‌رسان: {u.platform_id}\nوضعیت: {u.state}\n"
                f"جنسیت: {u.gender or '-'}\nبن: {ban_txt}\nعضویت: {u.created_at:%Y-%m-%d}")
        kb = [[Button("بن ۲۴ساعته", f"adm:ban:{uid}:24"), Button("بن ۷روزه", f"adm:ban:{uid}:168")],
              [Button("بن دائمی", f"adm:ban:{uid}:0"), Button("رفع بن", f"adm:unban:{uid}")],
              [Button("خروج از مکالمه", f"adm:kick:{uid}"), Button("حذف از صف", f"adm:dq:{uid}")],
              [Button("بازگشت به پنل", "adm:home")]]
        await app.say(chat_id, text, kb)

    async def _list_reports(self, chat_id: str) -> None:
        reps = await self.app.admin.open_reports()
        if not reps:
            return await self.app.say(chat_id, "گزارش بازی نیست.", self._back())
        kb = [[Button(f"گزارش #{r.id}", f"adm:rv:{r.id}")] for r in reps] + self._back()
        await self.app.say(chat_id, "گزارش‌های باز:", kb)

    async def _show_report(self, chat_id: str, rid: int) -> None:
        rep, msg = await self.app.admin.get_report(rid)
        if rep is None:
            return await self.app.say(chat_id, "گزارش پیدا نشد.", self._back())
        body = msg.text if msg and msg.text else "(پیام حذف شده)"
        text = (f"گزارش #{rep.id}\nگزارش‌دهنده: {rep.reporter_id}\nمتخلف: {rep.reported_id}\n"
                f"دلیل: {rep.reason}\n\nمتن پیام:\n{body}")
        await self.app.say(chat_id, text, [
            [Button("بن متخلف", f"adm:rban:{rid}"), Button("بستن گزارش", f"adm:rdis:{rid}")],
            [Button("بازگشت به پنل", "adm:home")]])

    async def _show_settings(self, chat_id: str) -> None:
        cur = await self.app.settings.all()
        text = (f"تنظیمات فعلی\n\nچت تصادفی: {'فعال' if cur['random_chat_enabled']=='1' else 'غیرفعال'}\n"
                f"لینک ناشناس: {'فعال' if cur['anon_links_enabled']=='1' else 'غیرفعال'}\n"
                f"مهلت اتصال: {cur['match_timeout']} ثانیه\n"
                f"محدودیت پیام: {cur['rate_limit_messages']} پیام در {cur['rate_limit_window']} ثانیه\n"
                f"متن خوشامد:\n{cur['welcome_text']}")
        kb = [[Button("تغییر چت تصادفی", "adm:tg:random_chat_enabled"),
               Button("تغییر لینک ناشناس", "adm:tg:anon_links_enabled")],
              [Button("مهلت اتصال", "adm:ed:match_timeout")],
              [Button("تعداد پیام مجاز", "adm:ed:rate_limit_messages"),
               Button("بازه (ثانیه)", "adm:ed:rate_limit_window")],
              [Button("متن خوشامد", "adm:ed:welcome_text")],
              [Button("بازگشت به پنل", "adm:home")]]
        await self.app.say(chat_id, text, kb)
