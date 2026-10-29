"""کلاینت جعلی درون‌حافظه‌ای با همان رابط supabase-py (query builder + rpc).

توابع rpc همان منطق supabase_schema.sql را در پایتون تکرار می‌کنن تا تست‌ها بدون
شبکه اجرا بشن. همهٔ عملیات زیر یک RLock اجرا می‌شن (مثل اتمیک بودن تراکنش).
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from postgrest.exceptions import APIError


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


UNIQUE = {
    "users": [("platform_id",)],
    "anonymous_links": [("token",)],
    "blocks": [("blocker_id", "blocked_id")],
    "broadcast_recipients": [("job_id", "user_id")],
    "bot_settings": [("key",)],
    "matching_queue": [("user_id",)],
}
NO_ID = {"matching_queue", "bot_settings"}
DEFAULTS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "users": lambda: dict(chat_id=None, first_name=None, last_name=None, username=None, gender=None,
                          want=None, state="idle", state_data=None, last_seen=now_iso(),
                          created_at=now_iso(), updated_at=now_iso()),
    "anonymous_links": lambda: dict(revoked=False, created_at=now_iso(), updated_at=now_iso()),
    "matching_queue": lambda: dict(enqueued_at=now_iso(), created_at=now_iso(), updated_at=now_iso()),
    "active_chats": lambda: dict(active=True, started_at=now_iso(), ended_at=None,
                                 created_at=now_iso(), updated_at=now_iso()),
    "anonymous_messages": lambda: dict(link_id=None, parent_id=None, status="received", seen_at=None,
                                       created_at=now_iso(), updated_at=now_iso()),
    "blocks": lambda: dict(created_at=now_iso(), updated_at=now_iso()),
    "reports": lambda: dict(message_id=None, reason="", status="open", resolved_by=None,
                            created_at=now_iso(), updated_at=now_iso()),
    "bans": lambda: dict(reason="", until=None, revoked=False, created_by="",
                         created_at=now_iso(), updated_at=now_iso()),
    "broadcast_jobs": lambda: dict(status="draft", created_at=now_iso(), updated_at=now_iso()),
    "broadcast_recipients": lambda: dict(status="pending", created_at=now_iso(), updated_at=now_iso()),
    "admin_audit_logs": lambda: dict(target="", detail="", created_at=now_iso()),
    "bot_settings": lambda: dict(updated_at=now_iso()),
    "message_logs": lambda: dict(message_id=None, kind="anon", sender_name="", sender_username="",
                                 recipient_name="", recipient_username="", text="", created_at=now_iso()),
}


class Resp:
    def __init__(self, data: Any, count: Optional[int] = None) -> None:
        self.data, self.count = data, count


def _coerce(sample: Any, raw: str) -> Any:
    if isinstance(sample, bool):
        return raw.lower() == "true"
    if isinstance(sample, int):
        return int(raw)
    return raw


def _cmp(op: str, a: Any, b: Any) -> bool:
    if op == "eq":
        return a == b
    if op == "neq":
        return a != b
    if a is None or b is None:
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def _or_pred(expr: str) -> Callable[[Dict[str, Any]], bool]:
    parts = []
    for item in expr.split(","):
        col, op, val = item.split(".", 2)
        parts.append((col, op, val))

    def pred(row: Dict[str, Any]) -> bool:
        for col, op, val in parts:
            cur = row.get(col)
            if op == "is":
                if (val == "null" and cur is None):
                    return True
            elif cur is not None and _cmp(op, cur, _coerce(cur, val)):
                return True
        return False
    return pred


class Request:
    def __init__(self, db: "FakeSupabase", table: str) -> None:
        self.db, self.table = db, table
        self.mode = "select"
        self.cols = "*"
        self.want_count = False
        self.payload: Any = None
        self.on_conflict: Optional[str] = None
        self.ignore_dups = False
        self.preds: List[Callable[[Dict[str, Any]], bool]] = []
        self.order_by: Optional[tuple] = None
        self.slice: Optional[tuple] = None

    # --- verbs ---
    def select(self, cols: str = "*", count: Optional[str] = None):
        self.cols, self.want_count = cols, bool(count)
        return self

    def insert(self, values):
        self.mode, self.payload = "insert", values
        return self

    def upsert(self, values, on_conflict: Optional[str] = None, ignore_duplicates: bool = False, **_):
        self.mode, self.payload = "upsert", values
        self.on_conflict, self.ignore_dups = on_conflict, ignore_duplicates
        return self

    def update(self, values):
        self.mode, self.payload = "update", values
        return self

    def delete(self):
        self.mode = "delete"
        return self

    # --- filters ---
    def _f(self, col: str, fn: Callable[[Any], bool]):
        self.preds.append(lambda r: fn(r.get(col)))
        return self

    def eq(self, col, val): return self._f(col, lambda x: x == val)
    def neq(self, col, val): return self._f(col, lambda x: x != val)
    def gt(self, col, val): return self._f(col, lambda x: x is not None and x > val)
    def gte(self, col, val): return self._f(col, lambda x: x is not None and x >= val)
    def lt(self, col, val): return self._f(col, lambda x: x is not None and x < val)
    def lte(self, col, val): return self._f(col, lambda x: x is not None and x <= val)
    def in_(self, col, vals): return self._f(col, lambda x: x in list(vals))
    def is_(self, col, val): return self._f(col, lambda x: x is None if val in ("null", None) else x == val)

    def or_(self, expr: str):
        self.preds.append(_or_pred(expr))
        return self

    def order(self, col: str, desc: bool = False):
        self.order_by = (col, desc)
        return self

    def limit(self, n: int):
        self.slice = (0, n - 1)
        return self

    def range(self, a: int, b: int):
        self.slice = (a, b)
        return self

    def execute(self) -> Resp:
        with self.db.lock:
            return self.db._run(self)


class FakeSupabase:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.tables: Dict[str, List[Dict[str, Any]]] = {t: [] for t in DEFAULTS}
        self.seq: Dict[str, int] = {}

    # --- پایه ---
    def table(self, name: str) -> Request:
        if name not in self.tables:
            raise APIError({"message": f"relation {name} does not exist", "code": "42P01",
                            "hint": None, "details": None})
        return Request(self, name)

    def rpc(self, name: str, params: Optional[Dict[str, Any]] = None):
        fn = getattr(self, "rpc_" + name)

        class _R:
            def execute(_self) -> Resp:
                with self.lock:
                    return Resp(copy.deepcopy(fn(**(params or {}))))
        return _R()

    def _violation(self, table: str) -> APIError:
        return APIError({"message": f'duplicate key value violates unique constraint on "{table}"',
                         "code": "23505", "hint": None, "details": None})

    def _check_unique(self, table: str, row: Dict[str, Any], skip: Optional[Dict[str, Any]] = None):
        for cols in UNIQUE.get(table, []):
            for other in self.tables[table]:
                if other is not skip and all(other.get(c) == row.get(c) for c in cols):
                    raise self._violation(table)

    def _new_row(self, table: str, values: Dict[str, Any]) -> Dict[str, Any]:
        row = DEFAULTS[table]()
        row.update(copy.deepcopy(values))
        if table not in NO_ID and "id" not in row:
            self.seq[table] = self.seq.get(table, 0) + 1
            row["id"] = self.seq[table]
        return row

    def _insert(self, table: str, values: Dict[str, Any]) -> Dict[str, Any]:
        row = self._new_row(table, values)
        self._check_unique(table, row)
        self.tables[table].append(row)
        return row

    @staticmethod
    def _project(rows: List[Dict[str, Any]], cols: str) -> List[Dict[str, Any]]:
        if cols.strip() == "*":
            return [copy.deepcopy(r) for r in rows]
        names = [c.strip() for c in cols.split(",")]
        return [{c: copy.deepcopy(r.get(c)) for c in names} for r in rows]

    def _run(self, q: Request) -> Resp:
        rows = self.tables[q.table]
        if q.mode == "insert":
            items = q.payload if isinstance(q.payload, list) else [q.payload]
            snapshot = copy.deepcopy(rows)
            try:
                out = [self._insert(q.table, v) for v in items]
            except APIError:
                self.tables[q.table][:] = snapshot
                raise
            return Resp(self._project(out, "*"))
        if q.mode == "upsert":
            items = q.payload if isinstance(q.payload, list) else [q.payload]
            keys = [c.strip() for c in (q.on_conflict or "id").split(",")]
            out = []
            for v in items:
                match = next((r for r in rows if all(r.get(k) == v.get(k) for k in keys)), None)
                if match is None:
                    out.append(self._insert(q.table, v))
                elif not q.ignore_dups:
                    match.update(copy.deepcopy(v))
                    out.append(match)
            return Resp(self._project(out, "*"))

        hit = [r for r in rows if all(p(r) for p in q.preds)]
        if q.mode == "update":
            for r in hit:
                new = {**r, **copy.deepcopy(q.payload)}
                self._check_unique(q.table, new, skip=r)
                r.update(q.payload)
            return Resp(self._project(hit, "*"))
        if q.mode == "delete":
            ids = {id(r) for r in hit}
            self.tables[q.table][:] = [r for r in rows if id(r) not in ids]
            return Resp(self._project(hit, "*"))

        if q.order_by:
            col, desc = q.order_by
            hit = sorted(hit, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        total = len(hit)
        if q.slice:
            hit = hit[q.slice[0]:q.slice[1] + 1]
        return Resp(self._project(hit, q.cols), total if q.want_count else None)

    # --- ابزار rpc ---
    def _find(self, table: str, **kw) -> Optional[Dict[str, Any]]:
        return next((r for r in self.tables[table] if all(r.get(k) == v for k, v in kw.items())), None)

    def _banned_ids(self) -> set:
        now = now_iso()
        return {b["user_id"] for b in self.tables["bans"]
                if not b["revoked"] and (b["until"] is None or b["until"] > now)}

    # --- rpc: منطق SQL ---
    def rpc_anon_join(self, p_user_id: int, p_timeout: int):
        me = self._find("users", id=p_user_id)
        if (me is None or me["gender"] not in ("male", "female")
                or me["want"] not in ("male", "female", "any")):
            return {"status": "invalid"}
        if me["state"] == "chatting":
            return {"status": "busy"}
        if self._find("matching_queue", user_id=p_user_id):
            return {"status": "queued"}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        me.update(state="waiting", state_data=None, updated_at=now.isoformat())
        self.tables["matching_queue"].append(self._new_row("matching_queue", dict(
            user_id=p_user_id, gender=me["gender"], want=me["want"], enqueued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=p_timeout)).isoformat())))
        cands = [q for q in self.tables["matching_queue"]
                 if q["user_id"] != p_user_id and q["expires_at"] > now.isoformat()
                 and (me["want"] == "any" or q["gender"] == me["want"])
                 and (q["want"] == "any" or q["want"] == me["gender"])]
        if not cands:
            return {"status": "queued"}
        cand = min(cands, key=lambda q: q["enqueued_at"])
        ids = {p_user_id, cand["user_id"]}
        self.tables["matching_queue"][:] = [q for q in self.tables["matching_queue"]
                                            if q["user_id"] not in ids]
        for uid in ids:
            u = self._find("users", id=uid)
            if u is None or u["state"] != "waiting":
                raise APIError({"message": "claim_failed", "code": "P0001", "hint": None, "details": None})
            u.update(state="chatting", state_data=None, updated_at=now.isoformat())
        chat = self._insert("active_chats", dict(user_a=p_user_id, user_b=cand["user_id"]))
        return {"status": "matched", "chat_id": chat["id"], "partner_id": cand["user_id"]}

    def rpc_anon_leave(self, p_user_id: int):
        before = len(self.tables["matching_queue"])
        self.tables["matching_queue"][:] = [q for q in self.tables["matching_queue"]
                                            if q["user_id"] != p_user_id]
        u = self._find("users", id=p_user_id)
        if u and u["state"] == "waiting":
            u.update(state="idle", updated_at=now_iso())
        return len(self.tables["matching_queue"]) < before

    def rpc_anon_sweep(self, p_now: str):
        gone = [q["user_id"] for q in self.tables["matching_queue"] if q["expires_at"] <= p_now]
        self.tables["matching_queue"][:] = [q for q in self.tables["matching_queue"]
                                            if q["user_id"] not in gone]
        for uid in gone:
            u = self._find("users", id=uid)
            if u and u["state"] == "waiting":
                u.update(state="idle", updated_at=now_iso())
        return gone

    def rpc_anon_repair(self):
        queued = {q["user_id"] for q in self.tables["matching_queue"]}
        chatting = {x for c in self.tables["active_chats"] if c["active"]
                    for x in (c["user_a"], c["user_b"])}
        for u in self.tables["users"]:
            if u["state"] == "waiting" and u["id"] not in queued:
                u["state"] = "idle"
            elif u["state"] == "chatting" and u["id"] not in chatting:
                u["state"] = "idle"

    def rpc_anon_end_chat(self, p_user_id: int):
        chat = next((c for c in self.tables["active_chats"]
                     if c["active"] and p_user_id in (c["user_a"], c["user_b"])), None)
        if chat is None:
            u = self._find("users", id=p_user_id)
            if u and u["state"] == "chatting":
                u.update(state="idle", state_data=None)
            return None
        chat.update(active=False, ended_at=now_iso(), updated_at=now_iso())
        for uid in (chat["user_a"], chat["user_b"]):
            u = self._find("users", id=uid)
            if u and u["state"] == "chatting":
                u.update(state="idle", state_data=None, updated_at=now_iso())
        return chat["user_b"] if chat["user_a"] == p_user_id else chat["user_a"]

    def rpc_anon_create_message(self, p_sender, p_recipient, p_text, p_link_id, p_parent_id):
        msg = self._insert("anonymous_messages", dict(
            sender_id=p_sender, recipient_id=p_recipient, text=p_text,
            link_id=p_link_id, parent_id=p_parent_id))
        s, r = self._find("users", id=p_sender) or {}, self._find("users", id=p_recipient) or {}

        def name(u):
            return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
        self._insert("message_logs", dict(
            message_id=msg["id"], kind="reply" if p_parent_id else "anon",
            sender_id=p_sender, sender_platform_id=s.get("platform_id", ""),
            sender_name=name(s), sender_username=s.get("username") or "",
            recipient_id=p_recipient, recipient_platform_id=r.get("platform_id", ""),
            recipient_name=name(r), recipient_username=r.get("username") or "", text=p_text))
        return msg

    def rpc_anon_purge_old(self, p_cutoff: str):
        used = {r["message_id"] for r in self.tables["reports"]
                if r["status"] == "open" and r["message_id"] is not None}
        parents = {m["parent_id"] for m in self.tables["anonymous_messages"] if m["parent_id"] is not None}
        keep = [m for m in self.tables["anonymous_messages"]
                if not (m["created_at"] < p_cutoff and m["id"] not in used and m["id"] not in parents)]
        n = len(self.tables["anonymous_messages"]) - len(keep)
        self.tables["anonymous_messages"][:] = keep
        return n

    def rpc_anon_audience_size(self):
        banned = self._banned_ids()
        return sum(1 for u in self.tables["users"] if u["id"] not in banned)

    def rpc_anon_broadcast_start(self, p_job_id: int):
        job = self._find("broadcast_jobs", id=p_job_id)
        if job is None or job["status"] != "draft":
            return False
        job.update(status="running", updated_at=now_iso())
        banned = self._banned_ids()
        for u in self.tables["users"]:
            if u["id"] not in banned and not self._find("broadcast_recipients", job_id=p_job_id, user_id=u["id"]):
                self._insert("broadcast_recipients", dict(job_id=p_job_id, user_id=u["id"]))
        return True

    def rpc_anon_stats(self):
        week = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).isoformat()
        t = self.tables
        return {
            "users": len(t["users"]),
            "active": sum(1 for u in t["users"] if u["last_seen"] >= week),
            "banned": len(self._banned_ids()),
            "chats": sum(1 for c in t["active_chats"] if c["active"]),
            "waiting": len(t["matching_queue"]),
            "anon": len(t["anonymous_messages"]),
            "bc_sent": sum(1 for r in t["broadcast_recipients"] if r["status"] == "sent"),
            "bc_failed": sum(1 for r in t["broadcast_recipients"] if r["status"] in ("failed", "blocked")),
            "reports": sum(1 for r in t["reports"] if r["status"] == "open"),
        }
