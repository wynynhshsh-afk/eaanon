#!/usr/bin/env python3
"""انتقال کامل دیتابیس SQLite قدیمی (bot.db) به Supabase، فقط با SUPABASE_URL و SUPABASE_KEY.

قبلش supabase_schema.sql را یک‌بار در SQL Editor اجرا کن. بعدش:

    python scripts/migrate_sqlite_to_supabase.py ./bot.db https://REF.supabase.co SERVICE_ROLE_KEY

(یا SUPABASE_URL و SUPABASE_KEY را در محیط/‏.env بگذار و فقط مسیر bot.db را بده.)
اجرای دوباره بی‌خطره؛ ردیف‌های تکراری نادیده گرفته می‌شن.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_dotenv  # noqa: E402
from database.session import Database  # noqa: E402

# به ترتیب وابستگی کلیدهای خارجی: (جدول، ستون‌های کلید برای on_conflict)
TABLES = [
    ("users", "id"), ("anonymous_links", "id"), ("matching_queue", "user_id"),
    ("active_chats", "id"), ("anonymous_messages", "id"), ("blocks", "id"), ("reports", "id"),
    ("bans", "id"), ("broadcast_jobs", "id"), ("broadcast_recipients", "id"),
    ("admin_audit_logs", "id"), ("bot_settings", "key"), ("message_logs", "id"),
]
BOOLEANS = {"revoked", "active"}
BATCH = 500


def _convert(row: dict) -> dict:
    return {k: (bool(v) if k in BOOLEANS and v is not None else v) for k, v in row.items()}


async def migrate(sqlite_path: str, url: str, key: str) -> dict:
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    db = Database(url, key)
    await db.check()
    existing = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts: dict = {}
    for table, conflict in TABLES:
        if table not in existing:
            continue
        rows = [_convert(dict(r)) for r in src.execute(f'SELECT * FROM "{table}"')]
        for i in range(0, len(rows), BATCH):
            await db.upsert(table, rows[i:i + BATCH], on_conflict=conflict, ignore_duplicates=True)
        counts[table] = len(rows)
    await db.rpc("anon_reset_sequences")  # id بعدی از آخرین id منتقل‌شده شروع بشه
    src.close()
    return counts


def main() -> None:
    load_dotenv()
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    path = args[0]
    url = args[1] if len(args) > 1 else os.environ.get("SUPABASE_URL", "")
    key = args[2] if len(args) > 2 else os.environ.get("SUPABASE_KEY", "")
    for table, n in asyncio.run(migrate(path, url.rstrip("/"), key)).items():
        print(f"{table}: {n}")


if __name__ == "__main__":
    main()
