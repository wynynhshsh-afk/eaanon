import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from adapters.base import AdapterError
from bot.ratelimit import RateLimiter
from database.models import (
    ActiveChat, AnonymousMessage, BroadcastRecipient, QueueEntry, User, utcnow,
)


async def user_row(h, pid):
    async with h.db.tx() as s:
        return (await s.execute(select(User).where(User.platform_id == str(pid)))).scalar_one()


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
    async with h.db.tx() as s:
        assert (await s.execute(select(func.count(QueueEntry.user_id)))).scalar_one() == 0
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
    async with h.db.tx() as s:
        first = (await s.get(QueueEntry, (await user_row(h, 1)).id)).expires_at
    await h.app.matcher.join((await user_row(h, 1)).id)
    async with h.db.tx() as s:
        again = (await s.get(QueueEntry, (await user_row(h, 1)).id)).expires_at
        n = (await s.execute(select(func.count(QueueEntry.user_id)))).scalar_one()
    assert first == again and n == 1


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
    async with h.db.tx() as s:
        chats = (await s.execute(select(ActiveChat))).scalars().all()
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


async def test_link_generate_recover_after_restart(h):
    await h.say(5, "/start")
    await h.click(5, "menu:link")
    text = h.fake.texts_to("5")[-1]
    token_url = [w for w in text.split() if w.startswith("https://")][0]
    assert "start=a_" in token_url
    h2 = await h.restart()
    await h2.click(5, "menu:link")
    assert token_url in h2.fake.texts_to("5")[-1]
    await h2.click(5, "link:new")
    new_url = [w for w in h2.fake.texts_to("5")[-1].split() if w.startswith("https://")][0]
    assert new_url != token_url
    await h2.say(6, "/start " + token_url.split("start=")[1])  # لینک باطل
    assert "معتبر نیست" in h2.fake.texts_to("6")[-1]
    await h2.db.dispose()


async def test_anonymous_message_seen_reply_flow(h):
    await h.say(10, "/start")
    await h.click(10, "menu:link")
    payload = [w for w in h.fake.texts_to("10")[-1].split() if w.startswith("https://")][0].split("start=")[1]
    await h.say(11, "/start " + payload)
    assert h.fake.texts_to("11")[-1] == "سلام! می‌تونی پیام ناشناست رو برای این شخص بفرستی."
    await h.say(11, "سلام ناشناس")
    assert "پیام ناشناس جدید" in h.fake.texts_to("10")[-1]
    mid = int(h.fake.buttons_to("10")[0].split(":")[1])
    async with h.db.tx() as s:
        assert (await s.get(AnonymousMessage, mid)).status == "delivered"
    assert "پیامت رو دید" not in " ".join(h.fake.texts_to("11"))  # تحویل ≠ دیده شدن
    await h.click(10, f"view:{mid}")
    assert "سلام ناشناس" in h.fake.texts_to("10")[-1]
    assert h.fake.texts_to("11")[-1] == "پیامت رو دید! 👀"
    await h.click(10, f"view:{mid}")  # بار دوم اطلاع تکراری نمی‌رود
    assert h.fake.texts_to("11").count("پیامت رو دید! 👀") == 1
    await h.click(10, f"reply:{mid}")
    await h.say(10, "جواب من")
    assert "پاسخ ناشناس" in h.fake.texts_to("11")[-1]
    async with h.db.tx() as s:
        assert (await s.get(AnonymousMessage, mid)).status == "replied"
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
    async with h.db.tx() as s:
        assert (await s.get(AnonymousMessage, mid)).text == ""


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
