# ربات چت ناشناس سروش پلاس

چت تصادفی، لینک ناشناس اختصاصی، صندوق پیام، پنل مالک و تبلیغات همگانی.
Python 3.9+ ، async (`aiohttp` + SQLAlchemy 2) ، PostgreSQL یا SQLite.

## ساختار
```
main.py              نقطه ورود (polling + کارهای پس‌زمینه)
config.py            تنظیمات از .env
adapters/            base.py (رابط) · soroush.py (واقعی) · fake.py (تست)
bot/                 handlers · matcher · chat_manager · anonymous · admin · advertising
                     keyboards · texts · settings · ratelimit · users
database/            models.py · session.py
tests/               ۱۷ تست خودکار
```

## نصب
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # سپس مقادیر را پر کن
```
در `.env` حداقل این‌ها را تنظیم کن:
- `SPLUS_BOT_TOKEN`: توکن از @BotFather سروش پلاس
- `OWNER_ID`: شناسه عددی تو. اگر نمی‌دانی، ربات را بدون آن اجرا کن، در چت خصوصی `/myid` بفرست، عدد را در `.env` بگذار و دوباره اجرا کن.

## دیتابیس
- **محلی:** پیش‌فرض SQLite است (`sqlite+aiosqlite:///./bot.db`) و جدول‌ها خودکار ساخته می‌شوند.
- **PostgreSQL:**
  ```bash
  sudo -u postgres createuser anonbot --pwprompt
  sudo -u postgres createdb anonbot -O anonbot
  ```
  و در `.env`: `DATABASE_URL=postgresql+asyncpg://anonbot:PASS@localhost:5432/anonbot`

## اجرا
```bash
python main.py
```
روی سرور با systemd (`/etc/systemd/system/anonbot.service`):
```ini
[Unit]
Description=Anon chat bot
After=network-online.target postgresql.service

[Service]
WorkingDirectory=/opt/anonbot
ExecStart=/opt/anonbot/.venv/bin/python main.py
Restart=always
User=anonbot

[Install]
WantedBy=multi-user.target
```
سپس `sudo systemctl enable --now anonbot`.

## تست
```bash
pytest
```

## نکات مهم و محدودیت‌ها
**تأییدشده از نمونه‌کد:** آدرس API، `getMe`، `getUpdates`، `sendMessage`، قالب پاسخ و ساختار پیام.

**تأییدنشده** (چون مستندات رسمی قابل دسترسی نبود؛ در `adapters/soroush.py` فرض Bot API شبیه تلگرام شده):
- دکمه‌های اینلاین (`reply_markup.inline_keyboard`) و رویداد `callback_query`
- `answerCallbackQuery` و `copyMessage` (برای ارسال عکس/ویدئو/ویس/فایل بین دو کاربر)
- قالب لینک شروع اختصاصی (`LINK_TEMPLATE` در `.env`)

اگر سرور سروش دکمه اینلاین را رد کند، ربات خودکار به **حالت متنی** برمی‌گردد: گزینه‌ها به شکل لیست نوشته می‌شوند و کاربر متن گزینه را می‌فرستد. اگر ارسال رسانه کار نکند، پیام «پشتیبانی نمی‌شه» نمایش داده می‌شود.
اگر لینک `?start=` کار نکند، کاربر می‌تواند `/start a_TOKEN` را مستقیم بفرستد.
با دیدن اولین خروجی واقعی، فقط `adapters/soroush.py` را اصلاح کن؛ منطق ربات به آن وابسته نیست.

**پیام «دیده شد»:** رویداد دیدن پیام وجود ندارد (یا تأیید نشده)، پس صاحب لینک اول یک اعلان با دکمه «مشاهده پیام» می‌گیرد. فقط با زدن آن (یا باز کردن پیام در صندوق) به فرستنده «پیامت رو دید! 👀» می‌رود، آن هم فقط یک‌بار.

**سیاست داده:** پیام حذف‌شده فوراً پاک‌سازی (متن خالی) می‌شود؛ پیام‌های قدیمی‌تر از `MESSAGE_RETENTION_DAYS` (پیش‌فرض ۹۰ روز) به‌طور خودکار حذف می‌شوند، مگر گزارش بازی روی آن‌ها باشد. متن چت تصادفی اصلاً ذخیره نمی‌شود.

**امنیت:** توکن لینک با `secrets.token_urlsafe` ساخته می‌شود؛ شناسه حساب هیچ‌وقت به طرف مقابل نمی‌رسد؛ مالک فقط با شناسه عددی `OWNER_ID` از API شناخته می‌شود؛ فایل‌های اجرایی (exe, apk, …) در چت مسدودند؛ محدودیت سرعت و ضد اسپم فعال است.

**چند نمونه هم‌زمان:** تطبیق با آپدیت شرطی دیتابیس و `FOR UPDATE SKIP LOCKED` (روی PostgreSQL) امن است. ولی محدودیت سرعت فعلاً در حافظه است؛ برای چند نمونه، `bot/ratelimit.py` را به Redis ببر. همچنین فقط **یک** نمونه باید polling کند.

**تبلیغات:** فعلاً فقط متنی است (ارسال تصویر/دکمه برای تبلیغات به تأیید API نیاز دارد). ارسال دسته‌ای، با سقف سرعت `BROADCAST_RATE`، بدون تکرار برای هر کاربر و قابل ادامه بعد از ری‌استارت.

## عیب‌یابی
| مشکل | راه‌حل |
|---|---|
| `SPLUS_BOT_TOKEN is not set` | `.env` را بساز و توکن را بگذار |
| ربات جواب نمی‌دهد | لاگ `polling failed` را ببین؛ اینترنت/توکن را چک کن |
| پنل مالک باز نمی‌شود | `OWNER_ID` عددی و درست باشد؛ `/myid` بزن |
| دکمه‌ها متنی شده‌اند | لاگ `inline keyboard rejected` یعنی API دکمه را نپذیرفته؛ ساختار را با مستندات تطبیق بده |
| `database is locked` | روی SQLite بار زیاد نبر؛ به PostgreSQL برو |
| لینک ناشناس باز نمی‌شود | `LINK_TEMPLATE` را با قالب رسمی لینک ربات سروش تطبیق بده |
