#!/usr/bin/env python3
"""ورودی اصلی - حالت Webhook (برای دیپلوی روی Render یا هر Web Service دیگه).

به‌جای poll_loop قبلی، یک سرور HTTP با aiohttp بالا میاد:
  - GET  /                 health check (برای Render)
  - POST {webhook_path}    آدرسی که setWebhook به سمتش اشاره می‌کنه؛ شامل
                            یک secret تصادفی در خودِ مسیر تا کسی جز سروش
                            نتونه آپدیت قلابی بفرسته.

تسک‌های دوره‌ای قبلی (sweep/broadcast/purge) به همون شکل، فقط به‌عنوان
background task های aiohttp، زنده نگه داشته می‌شن.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from adapters.base import AdapterError
from adapters.soroush import SoroushAdapter, parse_update
from bot.handlers import BotApp
from config import Config
from database.session import Database

log = logging.getLogger("main")

APP_KEY = web.AppKey("bot_app", BotApp)
CFG_KEY = web.AppKey("cfg", Config)
DB_KEY = web.AppKey("db", Database)
TASKS_KEY = web.AppKey("bg_tasks", list)


async def periodic(fn, interval: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await fn()
        except Exception:  # noqa: BLE001
            log.exception("periodic task failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def handle_webhook(request: web.Request) -> web.Response:
    cfg: Config = request.app[CFG_KEY]
    secret = request.match_info.get("secret", "")
    if not cfg.webhook_secret or secret != cfg.webhook_secret:
        return web.Response(status=404, text="not found")

    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="bad request")

    update = parse_update(raw)
    if update is not None:
        app: BotApp = request.app[APP_KEY]
        try:
            await app.handle_update(update)
        except Exception:  # noqa: BLE001
            log.exception("handle_update failed")

    # سروش منتظر 200 سریع می‌مونه؛ پردازش طولانی نباید ریسپانس رو بلاک کنه
    return web.Response(text="ok")


async def on_startup(app: web.Application) -> None:
    cfg = app[CFG_KEY]
    db = Database(cfg.database_url)
    await db.create_all()
    app[DB_KEY] = db

    adapter = SoroushAdapter(cfg.bot_token, cfg.api_base, cfg.poll_timeout)
    await adapter.start()

    bot = BotApp(cfg, db, adapter)
    await bot.matcher.repair()
    app[APP_KEY] = bot
    if not cfg.owner_id:
        log.warning("OWNER_ID is not set; the admin panel is disabled.")

    if cfg.webhook_url:
        full_url = cfg.webhook_url.rstrip("/") + cfg.webhook_path
        try:
            await adapter.set_webhook(full_url)
        except AdapterError as exc:
            log.error("setWebhook failed (code %s) - check WEBHOOK_URL / token", exc.code)
    else:
        log.warning("WEBHOOK_URL is not set; webhook was not registered with the platform.")

    stop = asyncio.Event()
    app["stop_event"] = stop

    async def purge() -> None:
        await bot.anon.purge_old(cfg.message_retention_days)
        bot.limiter.gc()

    app[TASKS_KEY] = [
        asyncio.create_task(periodic(bot.sweep_queue_once, 5, stop)),
        asyncio.create_task(periodic(bot.broadcast_tick, 3, stop)),
        asyncio.create_task(periodic(purge, 6 * 3600, stop)),
    ]
    log.info("bot @%s is running (webhook mode, path=%s)", adapter.bot_username, cfg.webhook_path)


async def on_cleanup(app: web.Application) -> None:
    stop: asyncio.Event = app["stop_event"]
    stop.set()
    tasks = app[TASKS_KEY] + list(app[APP_KEY]._bc_tasks.values())
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app[APP_KEY].adapter.close()
    await app[DB_KEY].dispose()


def build_app() -> web.Application:
    cfg = Config.from_env()
    logging.basicConfig(level=cfg.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = web.Application()
    app[CFG_KEY] = cfg
    app.router.add_get("/", handle_health)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post(cfg.webhook_path, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    app = build_app()
    cfg: Config = app[CFG_KEY]
    web.run_app(app, host="0.0.0.0", port=cfg.port)


if __name__ == "__main__":
    main()
