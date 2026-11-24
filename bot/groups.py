"""تنبيهات المنبه في القروبات: التسجيل، النشر التلقائي (مع التثبيت وتنظيف الرسالة السابقة)،
لكل موعد: رسالة عند الفتح، وتنبيه أخير قبل الإغلاق، ورسالة عند الإغلاق (غير مثبّتة)،
وإدارة القروبات من المدير بالأمر /groups.

يُسجَّل القروب فقط إذا أضاف البوتَ إليه أحدُ المدراء، وإلا يخرج البوت منه تلقائيًا.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from functools import partial

from bot import telegram
from bot.alarm import GRACE, WEEKDAYS, clock, until_label
from bot.telegram import TelegramError
from bot.ui import Reply, btn, kb, url_btn
from bot.welcome import Welcome

UTC = timezone.utc
GROUP_TYPES = ("group", "supergroup")


def _who(user: dict) -> str:
    name = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip() or "شخص"
    return f"{name} (@{user['username']})" if user.get("username") else name


def _perms(member: dict) -> tuple[bool, bool]:
    """(يقدر يثبّت، يقدر يحذف) من بيانات عضوية البوت في القروب."""
    if member.get("status") != "administrator":
        return False, False
    return bool(member.get("can_pin_messages")), bool(member.get("can_delete_messages"))


def _yes(flag) -> str:
    return "✅" if flag else "❌"


class Groups:
    def __init__(self, app):
        self.app = app
        self.welcome = Welcome(app)

    @property
    def alarm(self):
        return self.app.alarm

    @property
    def store(self):
        return self.app.store

    def notify_admins(self, text: str, markup: dict | None = None):
        for admin_id in self.app.admins:
            params = {"chat_id": admin_id, "text": text}
            if markup:
                params["reply_markup"] = markup
            try:
                self.app.api.call("sendMessage", **params)
            except TelegramError:
                pass

    # ---------------------------------------------------------- registration

    def on_member(self, upd: dict):
        """إضافة البوت لقروب أو إخراجه منه أو تغيير صلاحياته."""
        chat = upd.get("chat") or {}
        cid, title = chat["id"], chat.get("title", "")
        actor = upd.get("from") or {}
        member = upd.get("new_chat_member") or {}
        status = member.get("status")
        known = self.store.get_group(cid)

        if status in ("left", "kicked"):
            if known:
                self.store.del_group(cid)
                self.notify_admins(f"🚪 خرجت من القروب «{title}»، وحذفته من قائمة التنبيهات.")
            return
        if status not in ("member", "administrator", "restricted"):
            return

        pin, delete = _perms(member)
        if not known:
            if actor.get("id") not in self.app.admins:
                try:
                    self.app.api.call("leaveChat", chat_id=cid)
                except TelegramError:
                    pass
                self.notify_admins(f"⛔️ حاول {_who(actor)} إضافتي إلى القروب «{title}»، "
                                   "فخرجت منه تلقائيًا.")
                return
            self.store.set_group(cid, {"title": title, "on": True, "since": int(time.time()),
                                       "last": None, "pin": pin, "del": delete})
            head = (f"✅ تم تسجيل القروب «{title}».\n"
                    "سأنشر فيه تنبيهات الصفقة وجمع الأرباح تلقائيًا.")
        else:
            self.store.set_group(cid, {**known, "title": title, "pin": pin, "del": delete,
                                       "warn": ""})
            head = f"🔧 تحدّثت صلاحياتي في «{title}»."
        lines = [head, "", f"📌 التثبيت: {_yes(pin)}", f"🗑 الحذف: {_yes(delete)}"]
        if not (pin and delete):
            lines += ["", "⚠️ اجعلني مشرفًا بصلاحيتي التثبيت والحذف حتى أثبّت التنبيه "
                          "وأحذف التنبيه السابق تلقائيًا."]
        self.notify_admins("\n".join(lines))

    def on_chat_member(self, upd: dict):
        """انضمام عضو للقروب (يصل للبوت لأنه مشرف، حتى لو كانت رسائل الانضمام مخفية)."""
        old = (upd.get("old_chat_member") or {}).get("status")
        new = upd.get("new_chat_member") or {}
        joined = old in ("left", "kicked") and (
            new.get("status") in ("member", "administrator")
            or (new.get("status") == "restricted" and new.get("is_member", True)))
        if joined and new.get("user"):
            self.welcome.on_join(upd["chat"]["id"], [new["user"]])

    def on_group_message(self, msg: dict):
        """رسائل القروب: الانضمام للترحيب، وتحويل القروب لسوبر قروب (يتغير معرّفه)."""
        if msg.get("new_chat_members"):
            self.welcome.on_join(msg["chat"]["id"], msg["new_chat_members"])
        new_id = msg.get("migrate_to_chat_id")
        old = self.store.get_group(msg["chat"]["id"])
        if new_id and old:
            self.store.del_group(msg["chat"]["id"])
            self.store.set_group(new_id, {**old, "last": None})

    def lost(self, cid: int, reason: str = ""):
        """البوت طُرد من القروب أو حُذف القروب."""
        g = self.store.get_group(cid)
        if g:
            self.store.del_group(cid)
            self.notify_admins(f"🚪 لم أعد أستطيع الإرسال في «{g.get('title', '')}»، "
                               f"فحذفته من القائمة.{(' ' + reason) if reason else ''}")

    def _warn(self, cid: int, text: str):
        """يحفظ مشكلة الصلاحيات ويبلّغ المدير مرة واحدة فقط لكل مشكلة جديدة."""
        g = self.store.get_group(cid)
        if g and g.get("warn") != text:
            self.store.set_group(cid, {**g, "warn": text})
            self.notify_admins(f"⚠️ القروب «{g.get('title', '')}»: {text}")

    # ---------------------------------------------------------------- texts

    def next_start(self, after: datetime):
        """أقرب موعد قادم (من أي نوع) بعد وقت معيّن: (الموعد، وقت بدايته)."""
        best = None
        for ev in self.alarm.events:
            for t in self.alarm._occurrences(ev, after, 0, 8):
                if t > after:
                    if best is None or t < best[1]:
                        best = (ev, t)
                    break
        return best

    def _next_line(self, end: datetime) -> str:
        nxt = self.next_start(end)
        if not nxt:
            return ""
        ev, t = nxt
        name = ev["name"].removeprefix("موعد ").strip()
        days = (t.date() - end.date()).days
        if days == 0:
            return f"⏭ الموعد القادم: {name} الساعة {clock(t)} بتوقيت غرينتش"
        if days == 1:
            return f"⏭ نلقاكم بكرة الساعة {clock(t)} بتوقيت غرينتش"
        return f"⏭ الموعد القادم: {name} يوم {WEEKDAYS[t.weekday()]} الساعة {clock(t)} بتوقيت غرينتش"

    def text(self, ev: dict, start: datetime, kind: str, now: datetime) -> str:
        end = start + ev["dur"]
        emoji = ev.get("emoji", "")
        if kind == "end":
            head = f"🔒 انتهى {ev['name']} لليوم"
            nxt = self._next_line(end)
            if nxt:
                head += "\n" + nxt
            phrases = ev.get("group_end") or []
        elif kind == "close":
            head = (f"⏳ تنبيه أخير: {ev['name']} يُغلق {until_label(end - now)}!\n"
                    f"🕒 الإغلاق الساعة {clock(end)} بتوقيت غرينتش")
            phrases = ev.get("group_close") or []
        else:
            when = (f"من {clock(start)} حتى {clock(end)}" if ev["dur"]
                    else f"الساعة {clock(start)}")
            head = f"🔔 بدأ {ev['name']} الآن! {emoji}\n🕒 {when} بتوقيت غرينتش"
            phrases = ev.get("group_open") or []
        return head + (f"\n\n{random.choice(phrases)}" if phrases else "")

    def markup(self) -> dict | None:
        username = self.app.me.get("username")
        if not username:
            return None
        label = self.alarm.cfg.get("group_button") or "⏰ فعّل المنبه الخاص بك"
        return kb([[url_btn(label, f"https://t.me/{username}?start=alarm")]])

    # ------------------------------------------------------------- scheduler

    def tick(self, now: datetime):
        store, alarm = self.store, self.alarm
        self.welcome.tick(now.timestamp())
        if not any(g.get("on", True) for g in store.groups().values()):
            return
        ts = now.timestamp()
        for ei, ev in enumerate(alarm.events):
            for start in alarm._occurrences(ev, now):
                day, end = f"{start:%Y-%m-%d}", start + ev["dur"]
                if start <= now < start + alarm.main_limit(ev) and \
                        store.claim(f"{day}|{ev['id']}|g-open", ts):
                    self.app.run(partial(self.post, ei, start, "open", now))
                if alarm.closing and ev["dur"] > timedelta(minutes=alarm.closing):
                    fire_at = end - timedelta(minutes=alarm.closing)
                    if fire_at <= now < end - timedelta(minutes=1) and \
                            store.claim(f"{day}|{ev['id']}|g-close", ts):
                        self.app.run(partial(self.post, ei, start, "close", now))
                if ev["dur"] and end <= now < end + GRACE and \
                        store.claim(f"{day}|{ev['id']}|g-end", ts):
                    self.app.run(partial(self.post, ei, start, "end", now))

    def post(self, ei: int, start: datetime, kind: str, now: datetime):
        ev = self.alarm.events[ei]
        api, markup = self.app.api, self.markup()
        sent = 0
        for cid, g in self.store.groups().items():
            if not g.get("on", True):
                continue
            params = {"chat_id": cid, "text": self.text(ev, start, kind, now)}
            if markup:
                params["reply_markup"] = markup
            try:
                msg = api.call("sendMessage", **params)
            except TelegramError as e:
                if e.blocked or "kicked" in e.description or "not a member" in e.description:
                    self.lost(cid)
                else:
                    self._warn(cid, f"لم أستطع نشر التنبيه ({e.description}).")
                continue
            sent += 1
            # رسالة الإغلاق لا تُثبَّت: التثبيت فقط لموعد مفتوح الآن
            self._replace_pinned(cid, g, msg["message_id"], pin=kind != "end")
            time.sleep(telegram.SEND_DELAY)
        print(f"groups {ev['id']} {kind}: sent {sent}", flush=True)

    def _replace_pinned(self, cid: int, g: dict, new_id: int, pin: bool = True):
        """يحذف التنبيه السابق للبوت (فقط رسالة البوت نفسه)، ويثبّت الجديد بدون إشعار إضافي."""
        api, problems = self.app.api, []
        if g.get("last"):
            try:
                api.call("deleteMessage", chat_id=cid, message_id=g["last"])
            except TelegramError as e:
                if "not found" not in e.description:
                    problems.append("لا أملك صلاحية حذف الرسائل")
                    try:  # لم نستطع حذفها، فعلى الأقل نلغي تثبيتها
                        api.call("unpinChatMessage", chat_id=cid, message_id=g["last"])
                    except TelegramError:
                        pass
        if pin:
            try:
                api.call("pinChatMessage", chat_id=cid, message_id=new_id,
                         disable_notification=True)
            except TelegramError:
                problems.append("لا أملك صلاحية تثبيت الرسائل")
        fresh = self.store.get_group(cid) or g
        self.store.set_group(cid, {**fresh, "last": new_id})
        if problems:
            self._warn(cid, "، و".join(problems) + ". اجعلني مشرفًا بهاتين الصلاحيتين.")

    # ------------------------------------------------------------- admin UI

    def menu(self, toast: str | None = None) -> Reply:
        groups = self.store.groups()
        if not groups:
            return Reply("💬 لا توجد قروبات مسجلة بعد.\n\n"
                         "أضفني لأي قروب من حسابك، واجعلني مشرفًا بصلاحيتي التثبيت والحذف، "
                         "وسأسجّله تلقائيًا وأرسل لك تأكيدًا.", toast=toast)
        lines = [f"💬 القروبات المسجلة ({len(groups)})", ""]
        rows = []
        for cid, g in groups.items():
            icon = "🟢" if g.get("on", True) else "⏸"
            lines.append(f"{icon} {g.get('title', '')}" + (" ⚠️" if g.get("warn") else ""))
            rows.append([btn(f"{icon} {g.get('title', '')[:40]}", f"gr:{cid}")])
        lines += ["", "اختر قروبًا لإدارته 👇"]
        return Reply("\n".join(lines), kb(rows), toast)

    def _refresh_perms(self, cid: int, g: dict) -> dict:
        bot_id = self.app.me.get("id")
        if not bot_id:
            return g
        try:
            member = self.app.api.call("getChatMember", chat_id=cid, user_id=bot_id)
        except TelegramError:
            return g
        pin, delete = _perms(member)
        g = {**g, "pin": pin, "del": delete}
        if pin and delete:
            g["warn"] = ""
        self.store.set_group(cid, g)
        return g

    def detail(self, cid: int, toast: str | None = None) -> Reply:
        g = self.store.get_group(cid)
        if not g:
            return self.menu(toast="هذا القروب لم يعد مسجلًا")
        g = self._refresh_perms(cid, g)
        on = g.get("on", True)
        lines = [f"💬 {g.get('title', '')}", "",
                 f"الحالة: {'🟢 التنبيهات مفعّلة' if on else '⏸ التنبيهات متوقفة'}",
                 f"👋 الترحيب بالأعضاء الجدد: {_yes(g.get('welcome', True))}",
                 f"📌 التثبيت: {_yes(g.get('pin'))}", f"🗑 الحذف: {_yes(g.get('del'))}"]
        if g.get("warn"):
            lines += ["", f"⚠️ {g['warn']}"]
        welcome = g.get("welcome", True)
        rows = [[btn("⏸ إيقاف التنبيهات" if on else "▶️ تفعيل التنبيهات", f"gr:{cid}:t")],
                [btn("👋 إيقاف الترحيب" if welcome else "👋 تفعيل الترحيب", f"gr:{cid}:w")],
                [btn("🧪 رسالة تجريبية", f"gr:{cid}:x")],
                [btn("🚪 إخراج البوت من القروب", f"gr:{cid}:rm")],
                [btn("⬅️ القروبات", "gr")]]
        return Reply("\n".join(lines), kb(rows), toast)

    def on_callback(self, ctx, parts: list[str]) -> Reply:
        if not ctx.is_admin:
            raise ValueError("not an admin")
        if len(parts) == 1:
            return self.menu()
        cid = int(parts[1])
        act = parts[2] if len(parts) > 2 else ""
        g = self.store.get_group(cid)
        if act == "" or not g:
            return self.detail(cid)
        if act == "t":
            self.store.set_group(cid, {**g, "on": not g.get("on", True)})
            return self.detail(cid, toast="✅ تم الحفظ")
        if act == "w":
            self.store.set_group(cid, {**g, "welcome": not g.get("welcome", True)})
            return self.detail(cid, toast="✅ تم الحفظ")
        if act == "x":
            ev = self.alarm.events[0]
            now = datetime.now(UTC)
            start = next(t for t in self.alarm._occurrences(ev, now, 0, 8) if t > now)
            params = {"chat_id": cid, "text": "🧪 رسالة تجريبية، هكذا ستظهر التنبيهات:\n\n"
                                              + self.text(ev, start, "open", start)}
            if self.markup():
                params["reply_markup"] = self.markup()
            try:
                self.app.api.call("sendMessage", **params)
            except TelegramError as e:
                return Reply(toast=f"❌ لم أستطع الإرسال: {e.description}", alert=True)
            return Reply(toast="🧪 أرسلت رسالة تجريبية للقروب")
        if act == "rm":
            return Reply(f"🚪 هل تريد إخراج البوت من «{g.get('title', '')}»؟\n"
                         "ستتوقف تنبيهاته في هذا القروب.",
                         kb([[btn("نعم، أخرجه", f"gr:{cid}:rmy"), btn("✖️ لا", f"gr:{cid}")]]))
        if act == "rmy":
            self.store.del_group(cid)
            try:
                self.app.api.call("leaveChat", chat_id=cid)
            except TelegramError:
                pass
            return self.menu(toast="🚪 خرجت من القروب")
        raise ValueError(act)
