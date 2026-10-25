#!/usr/bin/env python3
"""منطق اصلی ربات - حالت Polling.

این فایل هیچ وابستگی‌ای به وب‌سرور نداره؛ مستقل هم قابل اجراست
(`python main.py`) و هم به‌عنوان یک تسک پس‌زمینه از start.py صدا زده می‌شه.
کل چرخه‌ی getUpdates -> handle_update و تسک‌های دوره‌ای (sweep/broadcast/
purge) اینجاست.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from adapters.soroush import SoroushAdapter
from bot.handlers import BotApp
from config import Config
from database.session import Database

log = logging.getLogger("main")


async def poll_loop(app: BotApp, stop: asyncio.Event) -> None:
    backoff = 1.0
    while not stop.is_set():
        try:
            updates = await app.adapter.get_updates()
            backoff = 1.0
            if updates:
                await asyncio.gather(*(app.handle_update(u) for u in updates))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("polling failed: %s", type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


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


async def run(stop: asyncio.Event | None = None) -> None:
    """ربات رو به‌صورت polling بالا می‌آره و تا ست‌شدن stop زنده نگه می‌داره.

    وقتی start.py این تابع رو صدا می‌زنه، stop خودش از بیرون پاس داده
    می‌شه تا کنترل خاموش‌شدن دست سرور وب بمونه. وقتی مستقیم اجرا بشه
    (python main.py)، یک Event داخلی با سیگنال‌های OS می‌سازه.
    """
    cfg = Config.from_env()
    logging.basicConfig(level=cfg.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(cfg.database_url)
    await db.create_all()

    adapter = SoroushAdapter(cfg.bot_token, cfg.api_base, cfg.poll_timeout)
    await adapter.start()
    # اگه قبلاً روی یه دیپلوی دیگه webhook ست شده بود، باید پاکش کنیم
    # وگرنه سروش به getUpdates جواب نمی‌ده.
    await adapter.delete_webhook()

    app = BotApp(cfg, db, adapter)
    await app.matcher.repair()
    if not cfg.owner_id:
        log.warning("OWNER_ID is not set; the admin panel is disabled.")

    owns_stop = stop is None
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass

    async def purge() -> None:
        await app.anon.purge_old(cfg.message_retention_days)
        app.limiter.gc()

    tasks = [
        asyncio.create_task(poll_loop(app, stop)),
        asyncio.create_task(periodic(app.sweep_queue_once, 5, stop)),
        asyncio.create_task(periodic(app.broadcast_tick, 3, stop)),
        asyncio.create_task(periodic(purge, 6 * 3600, stop)),
    ]
    log.info("bot @%s is running (polling mode)", adapter.bot_username)
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        for t in tasks + list(app._bc_tasks.values()):
            t.cancel()
        await asyncio.gather(*tasks, *app._bc_tasks.values(), return_exceptions=True)
        await adapter.close()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(run())
