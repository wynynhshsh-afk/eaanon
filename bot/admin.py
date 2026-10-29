from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

from adapters.base import Button, Keyboard
from database.models import AnonymousMessage, Ban, MessageLog, Report, User, iso, utcnow
from database.session import Database, eq
from .backup import build_backup_html
from .users import at_username, full_name

if TYPE_CHECKING:  # pragma: no cover
    from .handlers import BotApp

log = logging.getLogger("admin")


class AdminService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def audit(self, admin_id: str, action: str, target: str = "", detail: str = "") -> None:
        await self.db.insert("admin_audit_logs", {
            "admin_id": admin_id, "action": action, "target": target, "detail": detail[:500]})

    async def stats(self) -> dict:
        data = await self.db.rpc("anon_stats") or {}
        keys = ("users", "active", "banned", "chats", "waiting", "anon",
                "bc_sent", "bc_failed", "reports")
        return {k: int(data.get(k, 0)) for k in keys}

    async def ban(self, admin_id: str, user_id: int, reason: str, hours: Optional[int]) -> Ban:
        until = utcnow() + timedelta(hours=hours) if hours else None
        ban = await self.db.insert("bans", {
            "user_id": user_id, "reason": reason[:300], "until": iso(until),
            "created_by": admin_id}, Ban)
        await self.audit(admin_id, "ban", str(user_id), f"{reason} / {hours or 'permanent'}h")
        return ban

    async def unban(self, admin_id: str, user_id: int) -> int:
        rows = await self.db.update("bans", {"revoked": True}, eq(user_id=user_id, revoked=False))
        await self.audit(admin_id, "unban", str(user_id))
        return len(rows)

    async def open_reports(self, limit: int = 5):
        return await self.db.rows("reports", Report, where=eq(status="open"),
                                  order="id", limit=limit)

    async def get_report(self, rid: int):
        rep = await self.db.one("reports", Report, where=eq(id=rid))
        msg = None
        if rep and rep.message_id:
            msg = await self.db.one("anonymous_messages", AnonymousMessage,
                                    where=eq(id=rep.message_id))
        return rep, msg

    async def get_report_full(self, rid: int):
        """(گزارش، متن پیام، گزارش‌دهنده/گیرنده، متخلف/فرستنده) یا None."""
        rep, msg = await self.get_report(rid)
        if rep is None:
            return None
        body = msg.text if msg and msg.text else ""
        if not body and rep.message_id:  # گیرنده پیام رو حذف کرده؛ از آرشیو برمی‌داریم
            log_row = await self.db.one("message_logs", MessageLog,
                                        where=eq(message_id=rep.message_id), order="id", desc=True)
            body = log_row.text if log_row else ""
        reporter = await self.db.one("users", User, where=eq(id=rep.reporter_id))
        reported = await self.db.one("users", User, where=eq(id=rep.reported_id))
        return rep, body, reporter, reported, (msg.created_at if msg else None)

    async def close_report(self, admin_id: str, rid: int, action: str) -> None:
        await self.db.update("reports", {"status": "closed", "resolved_by": admin_id}, eq(id=rid))
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
                [Button("تنظیمات", "adm:set"), Button("بکاپ کاربران", "adm:backup")],
                [Button("خروج از پنل", "menu:home")]]

    async def open(self, user: User, chat_id: str) -> None:
        await self.app.users.set_state(user.id, "idle")
        await self.app.say(chat_id, "پنل مالک", self.home_kb())

    def _back(self) -> Keyboard:
        return [[Button("بازگشت به پنل", "adm:home")]]

    async def _set(self, user: User, state: str, **data) -> None:
        """وضعیت مرحله‌ای پنل؛ شناسهٔ پیام فعلی هم ذخیره می‌شه تا جواب متنی بعدی
        همون پیام رو ویرایش کنه."""
        data["pm"] = self.app.current_message_id()
        await self.app.users.set_state(user.id, state, data)

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
            await self._set(user, "admin_find")
            return await app.say(chat_id, "شناسه داخلی یا شناسه پیام‌رسان کاربر رو بفرست.", self._back())
        if act == "u":
            return await self._show_user(chat_id, int(parts[2]))
        if act == "ban":
            await self._set(user, "admin_ban_reason", uid=int(parts[2]), hours=int(parts[3]))
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
            await self._set(user, "admin_bc")
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
            await app.users.set_state(user.id, "idle")
            return await self._show_report(chat_id, int(parts[2]))
        if act == "rban":
            rep, _ = await app.admin.get_report(int(parts[2]))
            if rep:
                await app.ban_user(pid, rep.reported_id, "گزارش تخلف", None)
                await app.admin.close_report(pid, rep.id, "ban")
            return await self._show_report(chat_id, int(parts[2]),
                                           note="✅ کاربر متخلف بن شد و گزارش بسته شد.")
        if act == "rdis":
            await app.admin.close_report(pid, int(parts[2]), "dismiss")
            return await self._show_report(chat_id, int(parts[2]), note="گزارش بسته شد.")
        if act == "rpreply":
            await self._set(user, "admin_report_reply", rid=int(parts[2]))
            return await app.say(chat_id, "متنی که می‌خوای برای دریافت‌کنندهٔ پیام فرستاده بشه رو بنویس.",
                                 [[Button("انصراف", f"adm:rv:{parts[2]}")]])
        if act == "rpinfo":
            full = await app.admin.get_report_full(int(parts[2]))
            if full is None:
                return await self._show_report(chat_id, int(parts[2]))
            rep, _, receiver, offender, _ = full
            ok = await app.notify(rep.reporter_id, (
                "مشخصات فرستندهٔ پیام گزارش‌شده (ارسال از طرف مدیریت):\n\n"
                f"نام: {full_name(offender)}\nیوزرنیم: {at_username(offender)}\n"
                f"شناسه: {offender.platform_id if offender else '-'}"), new=True)
            await app.admin.audit(pid, "report_send_info", str(rep.id))
            return await self._show_report(chat_id, rep.id, note=(
                "✅ مشخصات متخلف برای دریافت‌کننده ارسال شد." if ok
                else "⚠️ ارسال نشد (دریافت‌کننده ربات را مسدود کرده یا در دسترس نیست)."))
        if act == "backup":
            return await self._backup(user, chat_id)
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
            await self._set(user, "admin_edit", key=key)
            hint = (f"مقدار جدید ({self.EDITABLE[key][0]} تا {self.EDITABLE[key][1]}) رو بفرست."
                    if key in self.EDITABLE else "متن جدید رو بفرست (حداکثر ۵۰۰ کاراکتر).")
            return await app.say(chat_id, hint, self._back())

    async def on_text(self, user: User, chat_id: str, text: str) -> bool:
        """True یعنی پیام در همین پنل مصرف شد."""
        app = self.app
        data = app.users.data_of(user)
        pid = user.platform_id
        app.use_edit(chat_id, data.get("pm"))  # جواب متنی هم روی همون پیام پنل ویرایش می‌شه
        if user.state == "admin_report_reply":
            await app.users.set_state(user.id, "idle")
            full = await app.admin.get_report_full(int(data.get("rid", 0)))
            if full is None or not text.strip():
                await app.say(chat_id, "گزارش یا متن نامعتبره.", self._back())
                return True
            ok = await app.notify(full[0].reporter_id, f"پیام مدیریت ربات:\n\n{text.strip()[:3000]}",
                                  new=True)
            await app.admin.audit(pid, "report_reply", str(full[0].id), text[:100])
            await self._show_report(chat_id, full[0].id, note=(
                "✅ پیام برای دریافت‌کننده ارسال شد." if ok
                else "⚠️ ارسال نشد (دریافت‌کننده ربات را مسدود کرده یا در دسترس نیست)."))
            return True
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

    async def _backup(self, user: User, chat_id: str) -> None:
        app = self.app
        await app.say(chat_id, "⏳ در حال ساخت بکاپ…")  # همین پیام پنل ویرایش می‌شه
        html = await build_backup_html(app.db, app.adapter.bot_username)
        stamp = utcnow().strftime("%Y%m%d-%H%M")
        ok = await app.adapter.send_document(
            chat_id, f"anonbot-backup-{stamp}.html", html.encode("utf-8"),
            "بکاپ کاربران، پیام‌ها، بن‌ها و گزارش‌ها (HTML)")
        await app.admin.audit(user.platform_id, "backup", "", "ok" if ok else "failed")
        await app.say(chat_id, "✅ فایل بکاپ ارسال شد. برای مشاهده بازش کن."
                      if ok else "⚠️ ارسال فایل بکاپ ممکن نشد. لاگ ربات رو ببین.",
                      self._back(), new=True)

    async def report_view(self, rid: int, header: str = "", note: str = ""):
        """(متن، دکمه‌ها) یک گزارش با مشخصات کامل؛ یا None."""
        full = await self.app.admin.get_report_full(rid)
        if full is None:
            return None
        rep, body, receiver, offender, sent_at = full
        lines = []
        if note:
            lines += [note, ""]
        lines += [
            f"{header or 'گزارش تخلف'} #{rep.id}",
            "",
            f"نام ارسال‌کننده (متخلف): {full_name(offender)}",
            f"نام دریافت‌کننده: {full_name(receiver)}",
            f"یوزرنیم ارسال‌کننده: {at_username(offender)}",
            f"یوزرنیم دریافت‌کننده: {at_username(receiver)}",
            f"شناسهٔ ارسال‌کننده: {offender.platform_id if offender else '-'}",
            f"شناسهٔ دریافت‌کننده: {receiver.platform_id if receiver else '-'}",
        ]
        if sent_at:
            lines.append(f"تاریخ پیام: {sent_at:%Y-%m-%d %H:%M} UTC")
        lines += ["", "متن پیام:", body or "(متن در دسترس نیست)"]
        if rep.status != "open":
            lines += ["", f"وضعیت: بسته‌شده ({rep.resolved_by or '-'})"]
            return "\n".join(lines), self._back()
        kb = [[Button("بن کردن کاربر متخلف", f"adm:rban:{rid}")],
              [Button("پاسخ به دریافت‌کننده", f"adm:rpreply:{rid}")],
              [Button("ارسال مشخصات متخلف به دریافت‌کننده", f"adm:rpinfo:{rid}")],
              [Button("بستن گزارش", f"adm:rdis:{rid}"), Button("بازگشت به پنل", "adm:home")]]
        return "\n".join(lines), kb

    async def _list_reports(self, chat_id: str) -> None:
        reps = await self.app.admin.open_reports()
        if not reps:
            return await self.app.say(chat_id, "گزارش بازی نیست.", self._back())
        kb = [[Button(f"گزارش #{r.id}", f"adm:rv:{r.id}")] for r in reps] + self._back()
        await self.app.say(chat_id, "گزارش‌های باز:", kb)

    async def _show_report(self, chat_id: str, rid: int, note: str = "") -> None:
        view = await self.report_view(rid, note=note)
        if view is None:
            return await self.app.say(chat_id, "گزارش پیدا نشد.", self._back())
        await self.app.say(chat_id, view[0], view[1])

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
