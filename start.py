#!/usr/bin/env python3
"""نقطه‌ی ورود روی Render.

Render برای Web Service انتظار داره برنامه یه پورت HTTP باز کنه و به
health-check جواب بده؛ وگرنه دیپلوی رو ناموفق علامت می‌زنه و سرویس رو
خاموش می‌کنه. اما منطق اصلی ربات (main.py) polling-محوره و اصلاً پورتی
باز نمی‌کنه.

این فایل دقیقاً همین شکاف رو پر می‌کنه: یک سرور aiohttp کوچیک (به شکل
webhook/HTTP سرویس) بالا می‌آره که فقط وظیفه‌ش جواب‌دادن به health-check
و نگه‌داشتن پورت زنده‌ست؛ همزمان main.run() رو به‌عنوان یک تسک پس‌زمینه
توی همون event loop روشن می‌کنه تا ربات به حالت polling کار کنه.

یعنی: start.py = پوسته‌ی webhook برای رندر | main.py = مغز ربات با polling.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

import main as bot_main
from config import Config

log = logging.getLogger("start")

STOP_KEY = web.AppKey("stop_event", asyncio.Event)
TASK_KEY = web.AppKey("bot_task", asyncio.Task)


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def handle_webhook(request: web.Request) -> web.Response:
    """مسیر وب‌هوک به‌خاطر سازگاری نگه داشته شده؛ منبع واقعی آپدیت‌ها
    polling داخل main.py هست، پس اینجا فقط یک 200 برمی‌گردونیم و رد می‌شیم.
    """
    try:
        await request.json()
    except Exception:  # noqa: BLE001
        pass
    return web.Response(text="ok")


async def supervised_bot(stop: asyncio.Event) -> None:
    """اگه main.run() به‌خاطر خطای شبکه/توکن غلط/هر چیز غیرمنتظره crash کنه،
    کل پردازش رو با خودش پایین نکشه - health endpoint باید همیشه جواب بده
    تا رندر سرویس رو خاموش نکنه. با backoff دوباره امتحان می‌کنیم.
    """
    backoff = 2.0
    while not stop.is_set():
        try:
            await bot_main.run(stop)
            return  # stop شد و run() تمیز برگشت
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("bot crashed, restarting in %.0fs", backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)


async def on_startup(app: web.Application) -> None:
    stop = asyncio.Event()
    app[STOP_KEY] = stop
    app[TASK_KEY] = asyncio.create_task(supervised_bot(stop))
    log.info("bot task started (polling) behind the webhook HTTP shell")


async def on_cleanup(app: web.Application) -> None:
    app[STOP_KEY].set()
    task = app[TASK_KEY]
    try:
        await asyncio.wait_for(task, timeout=15)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post("/webhook", handle_webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level="INFO",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = build_app()
    port = Config.from_env(require_token=False).port
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
