"""ساخت بکاپ گرافیکی HTML از کاربران، پیام‌ها، بن‌ها و گزارش‌ها (فایل تکی، بدون وابستگی)."""
from __future__ import annotations

import re
from datetime import datetime
from html import escape
from typing import Dict, List

from database.models import Ban, MessageLog, Report, User, utcnow
from database.session import Database

MAX_MESSAGES = 5000


def _dt(value: datetime | None) -> str:
    return f"{value:%Y-%m-%d %H:%M}" if value else "-"


def _name(u: User | None) -> str:
    if u is None:
        return "نامشخص"
    return " ".join(x for x in (u.first_name, u.last_name) if x).strip() or "بدون نام"


def _uname(value: str | None) -> str:
    return f"@{value}" if value else "-"


def _e(value: object) -> str:
    return escape(str(value if value is not None else ""))


def _rows(rows: List[str], cols: int) -> str:
    return "".join(rows) or f'<tr><td class="empty" colspan="{cols}">موردی وجود ندارد</td></tr>'


PAGE = 1000  # سقف پیش‌فرض هر درخواست PostgREST


async def _all(db: Database, table: str, model, order: str, desc: bool = False,
               cols: str = "*") -> list:
    """همهٔ ردیف‌ها را صفحه‌صفحه می‌خونه."""
    out: list = []
    offset = 0
    while True:
        page = await db.rows(table, model, cols=cols, order=order, desc=desc,
                             limit=PAGE, offset=offset)
        out.extend(page)
        if len(page) < PAGE:
            return out
        offset += PAGE


async def build_backup_html(db: Database, bot_username: str = "") -> str:
    now = utcnow()
    users = await _all(db, "users", User, "id")
    bans = await _all(db, "bans", Ban, "id", desc=True)
    reports = await _all(db, "reports", Report, "id", desc=True)
    total_msgs = await db.count("message_logs")
    msgs = await db.rows("message_logs", MessageLog, order="id", desc=True, limit=MAX_MESSAGES)
    sent: Dict[int, int] = {}
    got: Dict[int, int] = {}
    for m in await _all(db, "message_logs", None, "id", cols="sender_id,recipient_id"):
        sent[m["sender_id"]] = sent.get(m["sender_id"], 0) + 1
        got[m["recipient_id"]] = got.get(m["recipient_id"], 0) + 1

    by_id: Dict[int, User] = {u.id: u for u in users}
    active_ban_users = {b.user_id for b in bans
                        if not b.revoked and (b.until is None or b.until > now)}
    open_reports = sum(1 for r in reports if r.status == "open")

    user_rows = []
    for u in users:
        banned = u.id in active_ban_users
        badge = '<span class="badge bad">بن‌شده</span>' if banned else '<span class="badge ok">فعال</span>'
        gender = {"male": "پسر", "female": "دختر"}.get(u.gender or "", "-")
        user_rows.append(
            f"<tr><td>{u.id}</td><td class='mono'>{_e(u.platform_id)}</td>"
            f"<td><b>{_e(_name(u))}</b></td><td class='mono'>{_e(_uname(u.username))}</td>"
            f"<td>{gender}</td><td>{sent.get(u.id, 0)}</td><td>{got.get(u.id, 0)}</td>"
            f"<td>{_dt(u.created_at)}</td><td>{_dt(u.last_seen)}</td><td>{badge}</td>"
            f"<td><button class='link' onclick=\"goMsgs('{_e(u.platform_id)}')\">پیام‌ها</button></td></tr>")

    msg_rows = []
    for m in msgs:
        kind = '<span class="badge info">پاسخ</span>' if m.kind == "reply" else '<span class="badge">ناشناس</span>'
        msg_rows.append(
            f"<tr><td>{_dt(m.created_at)}</td><td>{kind}</td>"
            f"<td><b>{_e(m.sender_name or 'بدون نام')}</b><br><span class='sub mono'>"
            f"{_e(_uname(m.sender_username))} · {_e(m.sender_platform_id)}</span></td>"
            f"<td><b>{_e(m.recipient_name or 'بدون نام')}</b><br><span class='sub mono'>"
            f"{_e(_uname(m.recipient_username))} · {_e(m.recipient_platform_id)}</span></td>"
            f"<td class='text'>{_e(m.text)}</td></tr>")

    ban_rows = []
    for b in bans:
        u = by_id.get(b.user_id)
        live = b.id and not b.revoked and (b.until is None or b.until > now)
        state = '<span class="badge bad">فعال</span>' if live else '<span class="badge">پایان‌یافته/لغو</span>'
        ban_rows.append(
            f"<tr><td>{b.id}</td><td><b>{_e(_name(u))}</b><br><span class='sub mono'>"
            f"{_e(_uname(u.username if u else ''))} · {_e(u.platform_id if u else b.user_id)}</span></td>"
            f"<td>{_e(b.reason or '-')}</td><td>{_dt(b.created_at)}</td>"
            f"<td>{_dt(b.until) if b.until else 'دائمی'}</td><td>{_e(b.created_by or '-')}</td><td>{state}</td></tr>")

    report_rows = []
    msg_text = {m.message_id: m.text for m in msgs if m.message_id}
    for r in reports:
        off, rec = by_id.get(r.reported_id), by_id.get(r.reporter_id)
        st = '<span class="badge warn">باز</span>' if r.status == "open" else '<span class="badge ok">بسته</span>'
        report_rows.append(
            f"<tr><td>{r.id}</td><td>{_dt(r.created_at)}</td>"
            f"<td><b>{_e(_name(off))}</b><br><span class='sub mono'>"
            f"{_e(_uname(off.username if off else ''))} · {_e(off.platform_id if off else r.reported_id)}</span></td>"
            f"<td><b>{_e(_name(rec))}</b><br><span class='sub mono'>"
            f"{_e(_uname(rec.username if rec else ''))} · {_e(rec.platform_id if rec else r.reporter_id)}</span></td>"
            f"<td class='text'>{_e(msg_text.get(r.message_id, ''))}</td><td>{st}</td></tr>")

    truncated = (f"<p class='note'>فقط {MAX_MESSAGES} پیام آخر از {total_msgs} پیام نمایش داده شده؛ "
                 f"بقیه در دیتابیس موجودند.</p>" if total_msgs > MAX_MESSAGES else "")
    stats = [("کاربران", len(users)), ("پیام‌های ثبت‌شده", total_msgs),
             ("بن‌های فعال", len(active_ban_users)), ("گزارش‌های باز", open_reports)]
    cards = "".join(f"<div class='card'><div class='num'>{n}</div><div class='lbl'>{l}</div></div>"
                    for l, n in stats)

    values = {
        "BOT": _e("@" + bot_username if bot_username else "ربات ناشناس"),
        "DATE": f"{now:%Y-%m-%d %H:%M} UTC", "CARDS": cards,
        "USERS": _rows(user_rows, 11), "MSGS": _rows(msg_rows, 5),
        "BANS": _rows(ban_rows, 7), "REPORTS": _rows(report_rows, 6),
        "TRUNC": truncated, "N_USERS": str(len(users)), "N_MSGS": str(len(msg_rows)),
        "N_BANS": str(len(bans)), "N_REPORTS": str(len(reports)),
    }
    # یک‌بار عبور: متن کاربران نمی‌تونه جای‌نگهدارها را دوباره جایگزین کنه
    return re.sub(r"%%(\w+)%%", lambda m: values.get(m.group(1), m.group(0)), _TEMPLATE)


_TEMPLATE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>بکاپ %%BOT%%</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0f1226;--panel:#181c3a;--panel2:#1f2447;--text:#eef0ff;--sub:#9aa3d6;--line:#2b3160;
--accent:#7c5cff;--accent2:#22d3ee;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b}
@media (prefers-color-scheme: light){:root{--bg:#f3f4fb;--panel:#fff;--panel2:#f0f1fa;--text:#1b1e3b;
--sub:#5d6491;--line:#dfe2f3}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:Vazirmatn,Tahoma,sans-serif;line-height:1.7}
header{padding:36px 24px 70px;background:linear-gradient(135deg,#6d4aff,#22d3ee);color:#fff}
header h1{margin:0;font-size:28px;font-weight:800}header p{margin:4px 0 0;opacity:.9}
main{max-width:1280px;margin:-48px auto 40px;padding:0 16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;
box-shadow:0 10px 30px rgba(0,0,0,.18)}
.num{font-size:32px;font-weight:800;background:linear-gradient(90deg,var(--accent),var(--accent2));
-webkit-background-clip:text;background-clip:text;color:transparent}
.lbl{color:var(--sub);font-size:14px}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 14px}
.tab{border:1px solid var(--line);background:var(--panel);color:var(--text);padding:9px 16px;
border-radius:999px;cursor:pointer;font:inherit}
.tab.on{background:linear-gradient(90deg,var(--accent),#5b8cff);color:#fff;border-color:transparent}
.tab small{opacity:.75;margin-right:6px}
.panel{display:none;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px}
.panel.on{display:block}
.search{width:100%;padding:11px 14px;border-radius:12px;border:1px solid var(--line);
background:var(--panel2);color:var(--text);font:inherit;margin-bottom:12px}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{position:sticky;top:0;background:var(--panel2);color:var(--sub);text-align:right;font-weight:600;
padding:10px;white-space:nowrap}
td{padding:10px;border-top:1px solid var(--line);vertical-align:top}
tbody tr:hover{background:rgba(124,92,255,.08)}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;direction:ltr;unicode-bidi:plaintext}
.sub{color:var(--sub);font-size:12px}
.text{white-space:pre-wrap;word-break:break-word;min-width:240px}
.empty{text-align:center;color:var(--sub);padding:30px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;background:var(--panel2);
border:1px solid var(--line)}
.badge.ok{color:var(--ok);border-color:var(--ok)}.badge.bad{color:var(--bad);border-color:var(--bad)}
.badge.warn{color:var(--warn);border-color:var(--warn)}.badge.info{color:var(--accent2);border-color:var(--accent2)}
.link{background:none;border:1px solid var(--accent);color:var(--accent);border-radius:8px;padding:2px 10px;
cursor:pointer;font:inherit;font-size:12px}
.note{color:var(--warn);font-size:13px}
footer{text-align:center;color:var(--sub);font-size:12px;padding:10px 0 30px}
</style>
</head>
<body>
<header><h1>بکاپ %%BOT%%</h1><p>تاریخ تهیه: %%DATE%%</p></header>
<main>
<div class="cards">%%CARDS%%</div>
<div class="tabs">
<button class="tab on" data-t="users">کاربران<small>%%N_USERS%%</small></button>
<button class="tab" data-t="msgs">پیام‌ها<small>%%N_MSGS%%</small></button>
<button class="tab" data-t="bans">بن‌ها<small>%%N_BANS%%</small></button>
<button class="tab" data-t="reports">گزارش‌ها<small>%%N_REPORTS%%</small></button>
</div>
<section id="users" class="panel on"><input class="search" data-for="tu" placeholder="جستجو در کاربران (نام، یوزرنیم، شناسه)…">
<div class="wrap"><table id="tu"><thead><tr><th>#</th><th>شناسه</th><th>نام</th><th>یوزرنیم</th><th>جنسیت</th><th>ارسالی</th><th>دریافتی</th><th>عضویت</th><th>آخرین فعالیت</th><th>وضعیت</th><th></th></tr></thead><tbody>%%USERS%%</tbody></table></div></section>
<section id="msgs" class="panel"><input class="search" id="msgsearch" data-for="tm" placeholder="جستجو در پیام‌ها (متن، نام، شناسه)…">%%TRUNC%%
<div class="wrap"><table id="tm"><thead><tr><th>تاریخ</th><th>نوع</th><th>مبدأ</th><th>مقصد</th><th>متن پیام</th></tr></thead><tbody>%%MSGS%%</tbody></table></div></section>
<section id="bans" class="panel"><input class="search" data-for="tb" placeholder="جستجو در بن‌ها…">
<div class="wrap"><table id="tb"><thead><tr><th>#</th><th>کاربر</th><th>دلیل</th><th>تاریخ</th><th>تا</th><th>توسط</th><th>وضعیت</th></tr></thead><tbody>%%BANS%%</tbody></table></div></section>
<section id="reports" class="panel"><input class="search" data-for="tr" placeholder="جستجو در گزارش‌ها…">
<div class="wrap"><table id="tr"><thead><tr><th>#</th><th>تاریخ</th><th>متخلف</th><th>گزارش‌دهنده</th><th>متن پیام</th><th>وضعیت</th></tr></thead><tbody>%%REPORTS%%</tbody></table></div></section>
</main>
<footer>این فایل شامل اطلاعات خصوصی کاربران است؛ آن را امن نگه دارید.</footer>
<script>
function show(t){document.querySelectorAll('.tab').forEach(function(b){b.classList.toggle('on',b.dataset.t===t)});
document.querySelectorAll('.panel').forEach(function(p){p.classList.toggle('on',p.id===t)})}
document.querySelectorAll('.tab').forEach(function(b){b.onclick=function(){show(b.dataset.t)}});
function filt(inp){var q=inp.value.trim().toLowerCase();
document.querySelectorAll('#'+inp.dataset.for+' tbody tr').forEach(function(r){
r.style.display=r.textContent.toLowerCase().indexOf(q)>-1?'':'none'})}
document.querySelectorAll('.search').forEach(function(i){i.oninput=function(){filt(i)}});
function goMsgs(pid){show('msgs');var i=document.getElementById('msgsearch');i.value=pid;filt(i);i.focus()}
</script>
</body></html>
"""
