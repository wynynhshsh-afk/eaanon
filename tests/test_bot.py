import asyncio
from datetime import timedelta

from adapters.base import AdapterError, IncomingMessage
from bot.ratelimit import RateLimiter
from database.models import (
    ActiveChat, AnonymousMessage, BroadcastRecipient, MessageLog, QueueEntry, User, utcnow,
)
from database.session import eq


async def user_row(h, pid):
    return await h.db.one("users", User, where=eq(platform_id=str(pid)))


async def queue_size(h):
    return await h.db.count("matching_queue")


async def test_new_user_gets_menu(h):
    await h.say(1, "/start")
    text, kb = h.fake.last_to("1")
    assert text == "حله!\nچه کاری برات انجام بدم؟"
    assert [b.data for r in kb for b in r] == ["menu:random", "menu:link", "menu:inbox", "menu:help"]


async def test_gender_saved_and_no_queue_before_valid_choice(h):
    await h.say(1, "/start")
    await h.click(1, "menu:random")
    assert h.fake.last_to("1")[0] == "جنسیت خودت رو مشخص کن."
    await h.click(1, "w:any")  # انتخاب نامعتبر در این مرحله
    assert await queue_size(h) == 0
    await h.click(1, "g:male")
    assert (await user_row(h, 1)).gender == "male"
    assert h.fake.last_to("1")[0] == "برات مهمه مخاطبت پسره یا دختر؟"


async def test_gender_matching_respected(h):
    await h.say(1, "/start"); await h.say(2, "/start"); await h.say(3, "/start")
    await h.connect(1, "male", "female")      # پسر، دنبال دختر
    await h.connect(2, "male", "any")         # پسر، فرقی نمی‌کنه → نباید به 1 وصل شه
    assert (await user_row(h, 1)).state == "waiting"
    assert (await user_row(h, 2)).state == "waiting"
    await h.connect(3, "female", "male")      # دختر، دنبال پسر → با 1 یا 2؛ 1 اولویت زمانی دارد
    assert (await user_row(h, 3)).state == "chatting"
    assert (await user_row(h, 1)).state == "chatting"
    assert (await user_row(h, 2)).state == "waiting"
    assert "وصل شدی! مکالمه ناشناست شروع شد." in h.fake.texts_to("1")[-1]


async def test_timeout_and_cleanup(h):
    await h.say(1, "/start")
    await h.connect(1, "male", "any")
    assert await h.app.sweep_queue_once(utcnow() + timedelta(seconds=60)) == 0
    assert await h.app.sweep_queue_once(utcnow() + timedelta(seconds=121)) == 1
    assert "فعلاً کاربری برای چت ناشناس پیدا نشد" in h.fake.texts_to("1")[-1]
    assert (await user_row(h, 1)).state == "idle"
    await h.connect(1, "male", "any")  # می‌تواند دوباره جستجو کند
    assert (await user_row(h, 1)).state == "waiting"


async def test_duplicate_join_does_not_reset_timer_or_double_connect(h):
    await h.say(1, "/start")
    await h.connect(1, "male", "any")
    uid = (await user_row(h, 1)).id
    first = (await h.db.one("matching_queue", QueueEntry, where=eq(user_id=uid))).expires_at
    await h.app.matcher.join(uid)
    again = (await h.db.one("matching_queue", QueueEntry, where=eq(user_id=uid))).expires_at
    assert first == again and await queue_size(h) == 1


async def test_concurrent_matching_no_double_connect(h):
    ids = list(range(100, 120))
    for i in ids:
        await h.say(i, "/start")
        await h.click(i, "menu:random")
        await h.click(i, "g:male" if i % 2 else "g:female")
        u = await user_row(h, i)
        await h.app.users.set_state(u.id, "choosing_want", want="any")
    rows = [await user_row(h, i) for i in ids]
    results = await asyncio.gather(*(h.app.matcher.join(u.id) for u in rows))
    assert sum(r.status == "matched" for r in results) == 10
    chats = await h.db.rows("active_chats", ActiveChat)
    members = [m for c in chats for m in (c.user_a, c.user_b)]
    assert len(members) == len(set(members)) == 20


async def test_relay_and_end_by_either_side(h):
    await h.say(1, "/start"); await h.say(2, "/start")
    await h.connect(1, "male", "any"); await h.connect(2, "female", "any")
    await h.say(1, "سلام")
    assert h.fake.texts_to("2")[-1] == "سلام"
    await h.say(2, "علیک")
    assert h.fake.texts_to("1")[-1] == "علیک"
    assert all("1" not in t for t in [h.fake.texts_to("2")[-1]])
    await h.click(2, "chat:end")
    assert "مکالمه تموم شد" in h.fake.texts_to("1")[-1]
    assert "مکالمه تموم شد" in h.fake.texts_to("2")[-1]
    before = len(h.fake.texts_to("2"))
    await h.say(1, "هنوز هستی؟")  # مکالمه تمام شده؛ نباید به نفر قبلی برسد
    assert len(h.fake.texts_to("2")) == before
    assert (await user_row(h, 1)).state == "idle"


async def test_media_relay_and_dangerous_file(h):
    await h.say(1, "/start"); await h.say(2, "/start")
    await h.connect(1, "male", "any"); await h.connect(2, "female", "any")
    await h.say(1, "", media_type="photo")
    assert h.fake.relayed
    await h.say(1, "", media_type="document", file_name="x.exe")
    assert "پشتیبانی نمی‌شه" in h.fake.texts_to("1")[-1]


async def test_link_is_numeric_id_and_survives_restart(h):
    await h.say(5, "/start")
    await h.click(5, "menu:link")
    url = [w for w in h.fake.texts_to("5")[-1].split() if w.startswith("https://")][0]
    assert url == "https://splus.ir/testbot?start=5"
    h2 = await h.restart()  # شبیه جابه‌جایی ربات: لینک فقط به شناسه عددی وابسته‌ست
    await h2.click(5, "menu:link")
    assert url in h2.fake.texts_to("5")[-1]
    await h2.say(6, "/start 5")
    assert h2.fake.texts_to("6")[-1] == "سلام! می‌تونی پیام ناشناست رو برای این شخص بفرستی."
    await h2.say(7, "/start 999999")  # کاربری با این شناسه وجود نداره
    assert "معتبر نیست" in h2.fake.texts_to("7")[-1]
    await h2.db.dispose()


async def test_legacy_token_link_still_works(h):
    await h.say(5, "/start")
    owner = await user_row(h, 5)
    link = await h.app.anon.get_link(owner.id)
    await h.say(6, f"/start a_{link.token}")
    assert h.fake.texts_to("6")[-1] == "سلام! می‌تونی پیام ناشناست رو برای این شخص بفرستی."


async def test_anonymous_message_seen_reply_flow(h):
    await h.say(10, "/start")
    await h.click(10, "menu:link")
    payload = [w for w in h.fake.texts_to("10")[-1].split() if w.startswith("https://")][0].split("start=")[1]
    await h.say(11, "/start " + payload)
    assert h.fake.texts_to("11")[-1] == "سلام! می‌تونی پیام ناشناست رو برای این شخص بفرستی."
    await h.say(11, "سلام ناشناس")
    assert "پیام ناشناس جدید" in h.fake.texts_to("10")[-1]
    mid = int(h.fake.buttons_to("10")[0].split(":")[1])
    assert (await h.db.one("anonymous_messages", AnonymousMessage, where=eq(id=mid))).status == "delivered"
    assert "پیامت رو دید" not in " ".join(h.fake.texts_to("11"))  # تحویل ≠ دیده شدن
    await h.click(10, f"view:{mid}")
    assert "سلام ناشناس" in h.fake.texts_to("10")[-1]
    assert h.fake.texts_to("11")[-1] == "پیامت رو دید! 👀"
    await h.click(10, f"view:{mid}")  # بار دوم اطلاع تکراری نمی‌رود
    assert h.fake.texts_to("11").count("پیامت رو دید! 👀") == 1
    await h.click(10, f"reply:{mid}")
    await h.say(10, "جواب من")
    assert "پاسخ ناشناس" in h.fake.texts_to("11")[-1]
    assert (await h.db.one("anonymous_messages", AnonymousMessage, where=eq(id=mid))).status == "replied"
    # کاربر دیگر نمی‌تواند پیام مال دیگری را ببیند
    await h.click(11, f"view:{mid}")
    assert "وجود نداره" in h.fake.texts_to("11")[-1]
    # بلاک
    await h.click(10, f"block:{mid}")
    await h.say(11, "/start " + payload)
    assert "امکان ارسال پیام" in h.fake.texts_to("11")[-1]


async def test_inbox_navigation_unread_and_delete(h):
    await h.say(10, "/start")
    await h.click(10, "menu:link")
    payload = [w for w in h.fake.texts_to("10")[-1].split() if w.startswith("https://")][0].split("start=")[1]
    for sender, text in ((20, "یک"), (21, "دو")):
        await h.say(sender, "/start " + payload)
        await h.say(sender, text)
    await h.click(10, "menu:inbox")
    assert "دو" in h.fake.texts_to("10")[-1] and "(1/2)" in h.fake.texts_to("10")[-1]
    assert "inbox:1" in h.fake.buttons_to("10")
    await h.click(10, "inbox:1")
    assert "یک" in h.fake.texts_to("10")[-1] and "خوانده‌نشده: 0" in h.fake.texts_to("10")[-1]
    mid = int([b for b in h.fake.buttons_to("10") if b.startswith("del:")][0].split(":")[1])
    await h.click(10, f"del:{mid}:1")
    assert (await h.db.one("anonymous_messages", AnonymousMessage, where=eq(id=mid))).text == ""


async def test_ban_unban_and_ban_evasion(h):
    await h.say(1, "/start"); await h.say(2, "/start")
    await h.connect(1, "male", "any"); await h.connect(2, "female", "any")
    u1 = await user_row(h, 1)
    await h.say("9000", "/admin")
    await h.click("9000", f"adm:ban:{u1.id}:0")
    await h.say("9000", "-")
    assert "مسدود" in h.fake.texts_to("1")[-1]
    assert "مکالمه تموم شد" in h.fake.texts_to("2")[-1]
    await h.say(1, "/start")
    assert "حساب شما مسدود شده" in h.fake.texts_to("1")[-1]
    await h.click("9000", f"adm:unban:{u1.id}")
    await h.say(1, "/start")
    assert h.fake.texts_to("1")[-1].startswith("حله!")


async def test_owner_panel_unauthorized(h):
    await h.say(1, "/start")
    await h.say(1, "/admin")
    assert h.fake.texts_to("1")[-1].startswith("حله!")
    await h.click(1, "adm:stats")
    assert all("کاربران:" not in t for t in h.fake.texts_to("1"))
    await h.say("9000", "/admin")
    assert h.fake.texts_to("9000")[-1] == "پنل مالک"
    await h.click("9000", "adm:stats")
    assert "کاربران:" in h.fake.texts_to("9000")[-1]


async def test_broadcast_resume_after_error_no_duplicates(h):
    for i in (1, 2, 3, 4):
        await h.say(i, "/start")
    await h.say("9000", "/admin")
    await h.click("9000", "adm:bc")
    await h.say("9000", "تبلیغ تست")
    job = int(h.fake.buttons_to("9000")[0].split(":")[2])
    await h.click("9000", f"adm:bcok:{job}")
    h.fake.fail_for = {"2"}  # کاربر 2 ربات را مسدود کرده
    calls = []
    orig = h.app._send_ad

    async def flaky(user, text):
        calls.append(user.platform_id)
        if len(calls) == 3:
            raise asyncio.CancelledError()  # شبیه کرش وسط ارسال
        await orig(user, text)

    try:
        await h.app.ads.run(job, flaky, rate=1000, batch=1)
    except asyncio.CancelledError:
        pass
    counts = await h.app.ads.counts(job)
    assert counts["pending"] > 0
    await h.app.ads.run(job, h.app._send_ad, rate=1000, batch=2)  # ادامه از نقطه توقف
    counts = await h.app.ads.counts(job)
    assert counts["pending"] == 0 and counts["blocked"] == 1
    for i in ("1", "3", "4"):
        assert h.fake.texts_to(i).count("تبلیغ تست") == 1
    assert await h.app.ads.status(job) == "done"


async def test_rate_limiter_and_flood(h):
    rl = RateLimiter()
    assert all(rl.allow("k", 3, 10, now=t) for t in (0, 1, 2))
    assert not rl.allow("k", 3, 10, now=3)
    assert rl.allow("k", 3, 10, now=11)
    await h.app.settings.set("rate_limit_messages", "3")
    await h.say(1, "/start")
    for _ in range(6):
        await h.say(1, "x")
    assert any("آروم‌تر" in t for t in h.fake.texts_to("1"))


async def test_state_survives_restart(h):
    await h.say(1, "/start"); await h.say(2, "/start")
    await h.connect(1, "male", "any"); await h.connect(2, "female", "any")
    h2 = await h.restart()
    await h2.app.matcher.repair()
    await h2.say(1, "بعد از ری‌استارت")
    assert h2.fake.texts_to("2")[-1] == "بعد از ری‌استارت"
    await h2.db.dispose()


async def test_settings_disable_random_chat(h):
    await h.say("9000", "/admin")
    await h.click("9000", "adm:tg:random_chat_enabled")
    await h.say(1, "/start")
    await h.click(1, "menu:random")
    assert "غیرفعاله" in h.fake.texts_to("1")[-1]


async def _setup_anon(h, owner=10, sender=11):
    await h.say(owner, "/start")
    await h.app.handle_update(IncomingMessage(
        chat_id=str(sender), user_id=str(sender), message_id="1", text="/start",
        first_name="Ali", last_name="Karimi", username="ali_k"))
    await h.app.handle_update(IncomingMessage(
        chat_id=str(owner), user_id=str(owner), message_id="1", text="/start",
        first_name="Sara", username="sara_r"))
    await h.say(sender, f"/start {owner}")
    await h.say(sender, "متن توهین‌آمیز")
    return int([b for b in h.fake.buttons_to(str(owner)) if b.startswith("view:")][0].split(":")[1])


async def test_buttons_edit_same_message_instead_of_sending(h):
    await h.say(1, "/start")
    sends = h.fake.new_sends
    await h.click(1, "menu:help")
    await h.click(1, "menu:home")
    await h.click(1, "menu:link")
    assert h.fake.new_sends == sends          # هیچ پیام جدیدی نرفت
    assert len(h.fake.edits) == 3 and all(e[1] == "1" for e in h.fake.edits)
    h.fake.edit_ok = False                     # اگه ویرایش ممکن نبود، پیام جدید می‌ره
    await h.click(1, "menu:help")
    assert h.fake.new_sends == sends + 1


async def test_admin_panel_navigation_edits_in_place(h):
    await h.say("9000", "/admin")
    sends = h.fake.new_sends
    await h.click("9000", "adm:stats")
    await h.click("9000", "adm:home")
    await h.click("9000", "adm:set")
    assert h.fake.new_sends == sends and len(h.fake.edits) == 3


async def test_messages_archived_with_full_details(h):
    mid = await _setup_anon(h)
    logs = await h.db.rows("message_logs", MessageLog)
    assert len(logs) == 1
    log = logs[0]
    assert (log.sender_platform_id, log.sender_name, log.sender_username) == ("11", "Ali Karimi", "ali_k")
    assert (log.recipient_platform_id, log.recipient_name, log.recipient_username) == ("10", "Sara", "sara_r")
    assert log.text == "متن توهین‌آمیز" and log.message_id == mid and log.created_at
    await h.click(10, f"del:{mid}:0")  # حذف توسط گیرنده، آرشیو می‌مونه
    assert [m.text for m in await h.db.rows("message_logs", MessageLog)] == ["متن توهین‌آمیز"]


async def test_report_goes_to_owner_with_actions(h):
    mid = await _setup_anon(h)
    await h.say("9000", "/start")
    await h.click(10, f"report:{mid}")
    text, kb = h.fake.last_to("9000")
    for part in ("Ali Karimi", "Sara", "@ali_k", "@sara_r", "متن توهین‌آمیز"):
        assert part in text
    datas = [b.data for r in kb for b in r]
    assert any(d.startswith("adm:rban:") for d in datas)
    assert any(d.startswith("adm:rpreply:") for d in datas)
    assert any(d.startswith("adm:rpinfo:") for d in datas)
    rid = int([d for d in datas if d.startswith("adm:rpinfo:")][0].split(":")[2])

    # ارسال مشخصات متخلف به دریافت‌کننده
    await h.click("9000", f"adm:rpinfo:{rid}")
    info = h.fake.texts_to("10")[-1]
    assert "Ali Karimi" in info and "@ali_k" in info
    # پاسخ مالک به دریافت‌کننده
    await h.click("9000", f"adm:rpreply:{rid}")
    await h.say("9000", "گزارشت بررسی شد")
    assert "گزارشت بررسی شد" in h.fake.texts_to("10")[-1]
    # بن متخلف
    await h.click("9000", f"adm:rban:{rid}")
    assert "مسدود" in h.fake.texts_to("11")[-1]
    await h.say(11, "/start")
    assert "حساب شما مسدود شده" in h.fake.texts_to("11")[-1]


async def test_backup_html_sent_to_owner(h):
    await _setup_anon(h)
    await h.say("9000", "/admin")
    await h.click("9000", "adm:backup")
    chat, name, data, _ = h.fake.documents[-1]
    html = data.decode("utf-8")
    assert chat == "9000" and name.startswith("anonbot-backup-") and name.endswith(".html")
    for part in ("Ali Karimi", "@sara_r", "متن توهین‌آمیز", "کاربران", "پیام‌ها"):
        assert part in html
    assert "<script>alert" not in html


async def test_backup_escapes_html(h):
    from adapters.base import IncomingMessage
    from bot.backup import build_backup_html
    await h.app.handle_update(IncomingMessage(
        chat_id="1", user_id="1", message_id="1", text="/start",
        first_name="<script>alert(1)</script>", username="x"))
    html = await build_backup_html(h.db, "testbot")
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_config_reads_only_supabase_url_and_key(monkeypatch):
    from config import Config
    monkeypatch.setenv("SPLUS_BOT_TOKEN", "t")
    monkeypatch.setenv("SUPABASE_URL", "https://abc.supabase.co/")
    monkeypatch.setenv("SUPABASE_KEY", "service-key")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = Config.from_env()
    assert cfg.supabase_url == "https://abc.supabase.co" and cfg.supabase_key == "service-key"
    assert not hasattr(cfg, "database_url")


def test_database_requires_url_and_key():
    import pytest
    from database.session import Database
    with pytest.raises(SystemExit):
        Database("", "")
