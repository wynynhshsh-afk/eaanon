"""ابزار حفظ فرمت پیام (بولد، ایتالیک، کد/mono، لینک‌دار و ...).

سرور پیام‌رسان فرمت متن را جدا از خود متن، به‌شکل لیست entity می‌فرسته:
    {"type": "bold", "offset": 0, "length": 4}
offset و length بر حسب UTF-16 code unit هستن (نه کاراکتر پایتون)؛ یعنی یک ایموجی ۲ واحده.
ربات همون لیست رو بدون دست‌کاری نوع‌ها نگه می‌داره و موقع تحویل به گیرنده برمی‌گردونه.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

Entities = List[Dict[str, Any]]

# entity ای که شناسهٔ کاربر حمل می‌کنه؛ هیچ‌وقت منتقل نمی‌شه.
_DROP_TYPES = {"text_mention"}
_DROP_KEYS = {"user"}
MAX_ENTITIES = 100


def u16len(text: str) -> int:
    """طول متن بر حسب UTF-16 code unit (همون واحد offset در entity ها)."""
    return len(text.encode("utf-16-le")) // 2


def _clean_one(raw: Any, limit: int, shift: int) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type", "")).strip()
    if not kind or kind in _DROP_TYPES:
        return None
    try:
        offset = int(raw["offset"]) - shift
        length = int(raw["length"])
    except (KeyError, TypeError, ValueError):
        return None
    if offset < 0:  # بخشی از entity تو فاصلهٔ ابتدای متن بوده که حذف شده
        length += offset
        offset = 0
    if offset + length > limit:
        length = limit - offset
    if length <= 0:
        return None
    out = {k: v for k, v in raw.items() if k not in _DROP_KEYS}
    out["offset"], out["length"] = offset, length
    return out


def clean_entities(entities: Any, text: str, shift: int = 0) -> Entities:
    """entity های نامعتبر یا بیرون از متن رو حذف و بقیه رو برش می‌ده."""
    if not isinstance(entities, list):
        return []
    limit = u16len(text)
    cleaned = (_clean_one(e, limit, shift) for e in entities[:MAX_ENTITIES])
    return [e for e in cleaned if e is not None]


def strip_with_entities(raw: str, entities: Any) -> Tuple[str, Entities]:
    """مثل raw.strip() ولی offset ها رو هم با فاصله‌های حذف‌شدهٔ ابتدای متن جابه‌جا می‌کنه."""
    stripped = raw.strip()
    lead = len(raw) - len(raw.lstrip())
    return stripped, clean_entities(entities, stripped, shift=u16len(raw[:lead]))


def dumps(entities: Entities) -> Optional[str]:
    return json.dumps(entities, ensure_ascii=False, separators=(",", ":")) if entities else None


def loads(value: Optional[str]) -> Entities:
    if not value:
        return []
    try:
        data = json.loads(value)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def mono(text: str, span: str) -> Entities:
    """entity از نوع code (mono، قابل کپی با یک تپ) برای اولین رخداد span داخل text."""
    idx = text.find(span)
    if idx < 0 or not span:
        return []
    return [{"type": "code", "offset": u16len(text[:idx]), "length": u16len(span)}]
