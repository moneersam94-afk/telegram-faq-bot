"""ترحيب الأعضاء الجدد في القروبات المسجلة.

- رسالة دافئة فيها اسم العضو (منشن) واسم القروب، وزر يفتح البوت في الخاص.
- من ينضمون معًا خلال ثوانٍ يُرحَّب بهم في رسالة واحدة.
- الترحيب يُحذف بعد مدة (delete_after_minutes)، أو فور وصول ترحيب جديد.
"""

from __future__ import annotations

import html
import random
import threading
import time

from bot.telegram import TelegramError
from bot.ui import kb, url_btn

SEEN_SECONDS = 600  # لا نرحّب بنفس الشخص مرتين إذا وصلنا خبر انضمامه من مصدرين


def _mention(user: dict) -> str:
    name = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip() or "صديقنا"
    return f'<a href="tg://user?id={user["id"]}">{html.escape(name)}</a>'


def _names(users: list[dict]) -> str:
    if len(users) <= 3:
        return " و".join(_mention(u) for u in users)
    return f"{_mention(users[0])} و{_mention(users[1])} و{len(users) - 2} آخرين"


class Welcome:
    def __init__(self, app):
        self.app = app
        self.delay = 4  # ثوانٍ لتجميع من ينضمون معًا في رسالة واحدة
        self._lock = threading.Lock()
        self._batches: dict[int, list] = {}
        self._seen: dict[tuple, float] = {}

    @property
    def cfg(self) -> dict:
        return self.app.content.get("group_welcome") or {}

    def enabled(self, group: dict) -> bool:
        return bool(self.cfg.get("phrases")) and group.get("welcome", True)

    def on_join(self, cid: int, users: list[dict]):
        group = self.app.store.get_group(cid)
        if not group or not self.enabled(group):
            return
        now, bot_id = time.time(), self.app.me.get("id")
        with self._lock:
            self._seen = {k: t for k, t in self._seen.items() if now - t < SEEN_SECONDS}
            fresh = []
            for user in users:
                key = (cid, user.get("id"))
                if user.get("is_bot") or user.get("id") == bot_id or key in self._seen:
                    continue
                self._seen[key] = now
                fresh.append(user)
            if not fresh:
                return
            batch = self._batches.setdefault(cid, [])
            first = not batch
            batch.extend(fresh)
        if not first:
            return  # مؤقّت هذه الدفعة يعمل أصلًا
        if self.delay:
            timer = threading.Timer(self.delay, self.flush, [cid])
            timer.daemon = True
            timer.start()
        else:
            self.flush(cid)

    def text(self, users: list[dict], group_title: str) -> str:
        key = "phrases" if len(users) == 1 else "phrases_many"
        template = random.choice(self.cfg.get(key) or self.cfg["phrases"])
        if group_title.strip().startswith(("قروب", "جروب", "كروب", "مجموعة")):
            template = template.replace("بقروب {group}", "ب{group}")  # بدل «بقروب قروب ...»
        return (html.escape(template, quote=False)
                .replace("{name}", _names(users))
                .replace("{group}", html.escape(group_title, quote=False)))

    def markup(self) -> dict | None:
        username = self.app.me.get("username")
        if not username:
            return None
        return kb([[url_btn(self.cfg.get("button") or "ℹ️ معلومات أكثر",
                            f"https://t.me/{username}?start=welcome")]])

    def flush(self, cid: int):
        with self._lock:
            users = self._batches.pop(cid, [])
        store = self.app.store
        group = store.get_group(cid)
        if not users or not group:
            return
        params = {"chat_id": cid, "text": self.text(users, group.get("title", "")),
                  "parse_mode": "HTML"}
        markup = self.markup()
        if markup:
            params["reply_markup"] = markup
        try:
            msg = self.app.api.call("sendMessage", **params)
        except TelegramError as e:
            print(f"welcome failed in {cid}: {e}", flush=True)
            return
        old = (group.get("welcome_msg") or {}).get("id")
        if old:
            self._delete(cid, old)  # ترحيب واحد فقط في القروب
        fresh = store.get_group(cid) or group
        store.set_group(cid, {**fresh, "welcome_msg": {"id": msg["message_id"],
                                                       "at": int(time.time())}})

    def tick(self, now_ts: float):
        """يحذف رسائل الترحيب التي مضى عليها أكثر من المدة المحددة."""
        limit = float(self.cfg.get("delete_after_minutes", 5)) * 60
        store = self.app.store
        for cid, group in store.groups().items():
            w = group.get("welcome_msg")
            if not w or now_ts - w.get("at", 0) < limit:
                continue
            self._delete(cid, w["id"])
            fresh = store.get_group(cid)
            if fresh and (fresh.get("welcome_msg") or {}).get("id") == w["id"]:
                fresh.pop("welcome_msg", None)
                store.set_group(cid, fresh)

    def _delete(self, cid: int, message_id: int):
        try:
            self.app.api.call("deleteMessage", chat_id=cid, message_id=message_id)
        except TelegramError:
            pass  # حُذفت يدويًا، أو لا توجد صلاحية حذف (ويبلّغ نظام التنبيهات عن الصلاحيات)
