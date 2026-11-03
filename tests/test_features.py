import asyncio

import pytest

from adapters.base import AdapterError, Button
from adapters.soroush import SoroushAdapter, parse_update
from bot import entities as E
from database.models import AnonymousMessage


async def _start_owner(h, owner=10):
    await h.say(owner, "/start")


def _msg_ids(h, chat, prefix):
    return [int(b.split(":")[1]) for b in h.fake.buttons_to(chat) if b.startswith(prefix)]


def _notice_total(h, chat="10"):
    total = 0
    for t in h.fake.texts_to(chat):
        if "پیام ناشناس جدید" not in t:
            continue
        head = t.split("پیام ناشناس")[0].replace("شما", "").strip()
        total += 1 if head == "یک" else int(head)
    return total


# ---------- /newmsg: همهٔ پیام‌های هم‌زمان ----------
async def test_newmsg_shows_every_unread_message_from_concurrent_senders(h, monkeypatch):
    monkeypatch.setattr("bot.handlers.NEWMSG_PACING", 0)
    await _start_owner(h)

    async def send(i):
        await h.say(100 + i, "/start 10")
        await h.say(100 + i, f"پیام شماره {i}")

    await asyncio.gather(*(send(i) for i in range(10)))

    # هر پیام دقیقاً یک‌بار اعلام شده، ولی نه ۱۰ اعلان جدا
    assert _notice_total(h) == 10
    assert sum("پیام ناشناس جدید" in t for t in h.fake.texts_to("10")) < 10

    before = len(h.fake.sent)
    await h.say(10, "/newmsg")
    shown = [(t, k) for c, t, k in h.fake.sent[before:] if c == "10"]
    assert sorted(t for t, _ in shown) == sorted(f"پیام شماره {i}" for i in range(10))
    for _, kb in shown:  # هر پیام دکمهٔ پاسخ و گزارش خودش رو داره
        data = [b.data for row in kb for b in row]
        assert any(d.startswith("reply:") for d in data) and any(d.startswith("report:") for d in data)
    for i in range(10):
        assert h.fake.texts_to(str(100 + i)).count("پیامت رو دید! 👀") == 1

    await h.say(10, "/newmsg")  # دیگه چیزی نمونده
    assert "پیام جدیدی نداری" in h.fake.texts_to("10")[-1]


async def test_newmsg_batches_and_continues(h, monkeypatch):
    monkeypatch.setattr("bot.handlers.NEWMSG_PACING", 0)
    monkeypatch.setattr("bot.handlers.NEWMSG_BATCH", 4)
    await _start_owner(h)
    for i in range(6):
        await h.say(200 + i, "/start 10")
        await h.say(200 + i, f"m{i}")
    await h.say(10, "/newmsg")
    texts = h.fake.texts_to("10")
    assert [t for t in texts if t.startswith("m")] == ["m0", "m1", "m2", "m3"]  # قدیمی‌ترین اول
    assert "2 پیام جدید دیگه" in texts[-1]
    await h.say(10, "/newmsg")
    assert [t for t in h.fake.texts_to("10") if t.startswith("m")][-2:] == ["m4", "m5"]


# ---------- حفظ فرمت ----------
async def test_formatting_survives_strip_emoji_and_drops_mentions(h, monkeypatch):
    monkeypatch.setattr("bot.handlers.NEWMSG_PACING", 0)
    await _start_owner(h)
    raw = "  😀 سلام bold"
    bold_at = E.u16len("  😀 سلام ")
    ents = [{"type": "bold", "offset": bold_at, "length": 4},
            {"type": "text_mention", "offset": 2, "length": 2, "user": {"id": 1}},
            {"type": "italic", "offset": 900, "length": 3}]
    await h.say(20, "/start 10")
    await h.say(20, raw, entities=ents)
    await h.say(10, "/newmsg")
    assert h.fake.texts_to("10")[-1] == "😀 سلام bold"
    assert h.fake.entities_to("10")[-1] == [
        {"type": "bold", "offset": E.u16len("😀 سلام "), "length": 4}]


async def test_link_entity_and_stored_json_roundtrip(h, monkeypatch):
    monkeypatch.setattr("bot.handlers.NEWMSG_PACING", 0)
    await _start_owner(h)
    ents = [{"type": "text_link", "offset": 0, "length": 4, "url": "https://example.com/x"},
            {"type": "code", "offset": 5, "length": 3}]
    await h.say(21, "/start 10")
    await h.say(21, "لینک mono", entities=ents)
    rows = await h.db.rows("anonymous_messages", AnonymousMessage)
    assert len(rows) == 1 and E.loads(rows[0].entities) == ents  # JSON واقعاً ذخیره شده
    await h.say(10, "/newmsg")
    assert h.fake.entities_to("10")[-1] == ents


async def test_reply_keeps_formatting_back_to_sender(h, monkeypatch):
    monkeypatch.setattr("bot.handlers.NEWMSG_PACING", 0)
    await _start_owner(h)
    await h.say(22, "/start 10")
    await h.say(22, "سوال")
    await h.say(10, "/newmsg")
    mid = _msg_ids(h, "10", "reply:")[-1]
    await h.click(10, f"reply:{mid}")
    await h.say(10, "جواب", entities=[{"type": "bold", "offset": 0, "length": 4}])
    await h.say(22, "/newmsg")
    assert h.fake.texts_to("22")[-1] == "جواب"
    assert h.fake.entities_to("22")[-1] == [{"type": "bold", "offset": 0, "length": 4}]


async def test_random_chat_relay_keeps_formatting(h):
    await h.connect(30, "male", "female")
    await h.connect(31, "female", "male")
    ents = [{"type": "italic", "offset": 0, "length": 3}]
    await h.say(30, "سلام دنیا", entities=ents)
    assert h.fake.texts_to("31")[-1] == "سلام دنیا"
    assert h.fake.entities_to("31")[-1] == ents


async def test_missing_db_column_degrades_to_plain_text(h, monkeypatch):
    monkeypatch.setattr("bot.handlers.NEWMSG_PACING", 0)
    h.app.anon.entities_enabled = False  # ستون روی دیتابیس واقعی نیست
    await _start_owner(h)
    await h.say(23, "/start 10")
    await h.say(23, "bold شد", entities=[{"type": "bold", "offset": 0, "length": 4}])
    await h.say(10, "/newmsg")
    assert h.fake.texts_to("10")[-1] == "bold شد"
    assert h.fake.entities_to("10")[-1] is None


# ---------- لینک mono ----------
async def test_anonymous_link_is_monospace_and_exact(h):
    await _start_owner(h, 5)
    await h.click(5, "menu:link")
    text, ents = h.fake.texts_to("5")[-1], h.fake.entities_to("5")[-1]
    url = "https://splus.ir/testbot?start=5"
    assert url in text and len(ents) == 1 and ents[0]["type"] == "code"
    units = text.encode("utf-16-le")
    assert units[2 * ents[0]["offset"]: 2 * (ents[0]["offset"] + ents[0]["length"])].decode("utf-16-le") == url


# ---------- ابزار entity ----------
def test_strip_with_entities_clamps_and_shifts():
    text, ents = E.strip_with_entities("  ab cd  ", [
        {"type": "bold", "offset": 0, "length": 4},    # با فاصلهٔ ابتدا شروع می‌شه
        {"type": "italic", "offset": 5, "length": 50},  # از انتهای متن بیرون می‌زنه
        {"type": "code", "offset": 80, "length": 2},    # کاملاً بیرون
        "garbage", {"type": "bold"},
    ])
    assert text == "ab cd"
    assert ents == [{"type": "bold", "offset": 0, "length": 2},
                    {"type": "italic", "offset": 3, "length": 2}]


# ---------- adapter سروش ----------
def test_parse_update_reads_entities_for_text_and_caption():
    base = {"chat": {"id": 1, "type": "private"}, "from": {"id": 2}, "message_id": 3}
    ents = [{"type": "bold", "offset": 0, "length": 2}]
    up = parse_update({"message": {**base, "text": "hi", "entities": ents}})
    assert up.entities == ents
    cap = parse_update({"message": {**base, "photo": [{}], "caption": "hi", "caption_entities": ents}})
    assert cap.entities == ents
    assert parse_update({"message": {**base, "text": "x"}}).entities == []


async def test_send_message_retries_without_entities_but_keeps_keyboard():
    ad = SoroushAdapter("t")
    calls = []

    async def fake_call(method, params=None, get=False):
        calls.append(dict(params))
        if "entities" in params:
            raise AdapterError("rejected", code=400, description="can't parse entities")
        return {"message_id": 7}

    ad._call = fake_call
    mid = await ad.send_message("1", "سلام", [[Button("ok", "x")]],
                                [{"type": "bold", "offset": 0, "length": 4}])
    assert mid == "7" and len(calls) == 2
    assert "entities" in calls[0] and "entities" not in calls[1]
    assert "reply_markup" in calls[1] and ad.inline_ok is True
