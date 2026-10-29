from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: str
    supabase_url: str
    supabase_key: str
    api_base: str
    link_template: str
    poll_timeout: int
    broadcast_rate: float
    allow_media: bool
    message_retention_days: int
    log_level: str
    webhook_url: str = ""
    webhook_path: str = ""
    webhook_secret: str = ""
    port: int = 8080

    @classmethod
    def from_env(cls, require_token: bool = True) -> "Config":
        load_dotenv()
        token = _str("SPLUS_BOT_TOKEN") or _str("BOT_TOKEN")
        if require_token and not token:
            raise SystemExit("SPLUS_BOT_TOKEN is not set (see .env.example).")
        owner = _str("OWNER_ID")
        if owner and not owner.isdigit():
            raise SystemExit("OWNER_ID must be the numeric user id.")
        secret = _str("WEBHOOK_SECRET")
        if not secret and token:
            # یک مقدار پایدار از خود توکن می‌سازیم تا نیازی به تنظیم دستی نباشه،
            # ولی override با WEBHOOK_SECRET همیشه ارجحه.
            import hashlib
            secret = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
        return cls(
            bot_token=token,
            owner_id=owner,
            supabase_url=_str("SUPABASE_URL").rstrip("/"),
            supabase_key=_str("SUPABASE_KEY"),
            api_base=_str("API_BASE", "https://api.splus.ir/bot"),
            link_template=_str(
                "LINK_TEMPLATE", "https://splus.ir/{bot_username}?start={payload}"
            ),
            poll_timeout=_int("POLL_TIMEOUT", 30),
            broadcast_rate=float(_int("BROADCAST_RATE", 10)),
            allow_media=_str("ALLOW_MEDIA", "1") not in ("0", "false", "no"),
            message_retention_days=_int("MESSAGE_RETENTION_DAYS", 90),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            webhook_url=_str("WEBHOOK_URL"),
            webhook_path=_str("WEBHOOK_PATH", f"/webhook/{secret}"),
            webhook_secret=secret,
            port=_int("PORT", 8080),
        )
