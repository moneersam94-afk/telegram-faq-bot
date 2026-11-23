"""منبه الصفقات اليومية: الاشتراك، الإعدادات، والجدولة.

المواعيد مكتوبة في content.json بتوقيت غرينتش (GMT)، وتُعرض لكل مشترك بتوقيت بلده.
"""

from __future__ import annotations

import math
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from functools import partial

from bot.telegram import TelegramError, send_many
from bot.ui import Reply, btn, chunk, home_row, kb

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

UTC = timezone.utc
GRACE = timedelta(minutes=10)  # إذا تأخر السيرفر عن الموعد أكثر من هذا، لا نرسل تنبيهًا قديمًا

# (الاسم، المنطقة الزمنية، الفرق الاحتياطي بالدقائق إن لم تتوفر بيانات المناطق)
COUNTRIES = [
    ("🇸🇦 السعودية", "Asia/Riyadh", 180),
    ("🇦🇪 الإمارات", "Asia/Dubai", 240),
    ("🇰🇼 الكويت", "Asia/Kuwait", 180),
    ("🇶🇦 قطر", "Asia/Qatar", 180),
    ("🇧🇭 البحرين", "Asia/Bahrain", 180),
    ("🇴🇲 عُمان", "Asia/Muscat", 240),
    ("🇯🇴 الأردن", "Asia/Amman", 180),
    ("🇮🇶 العراق", "Asia/Baghdad", 180),
    ("🇸🇾 سوريا", "Asia/Damascus", 180),
    ("🇱🇧 لبنان", "Asia/Beirut", 120),
    ("🇵🇸 فلسطين", "Asia/Hebron", 120),
    ("🇪🇬 مصر", "Africa/Cairo", 120),
    ("🇱🇾 ليبيا", "Africa/Tripoli", 120),
    ("🇹🇳 تونس", "Africa/Tunis", 60),
    ("🇩🇿 الجزائر", "Africa/Algiers", 60),
    ("🇲🇦 المغرب", "Africa/Casablanca", 60),
    ("🇾🇪 اليمن", "Asia/Aden", 180),
    ("🇸🇩 السودان", "Africa/Khartoum", 120),
    ("🇹🇷 تركيا", "Europe/Istanbul", 180),
    ("🇩🇪 ألمانيا", "Europe/Berlin", 60),
    ("🇬🇧 بريطانيا", "Europe/London", 0),
]
OFFSETS = [-300, -240, -180, 0, 60, 120, 180, 210, 240, 270, 300, 330, 360, 420, 480]

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس",
          "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
MIN_FORMS = ("دقيقة", "دقيقتين", "دقائق", "دقيقة")
HOUR_FORMS = ("ساعة", "ساعتين", "ساعات", "ساعة")
DAY_FORMS = ("يوم", "يومين", "أيام", "يومًا")


# ------------------------------------------------------------- text helpers


def ar_count(n: int, forms) -> str:
    one, two, few, many = forms
    if n == 1:
        return one
    if n == 2:
        return two
    if 3 <= n <= 10:
        return f"{n} {few}"
    return f"{n} {many}"


def minutes_label(total: int) -> str:
    days, rest = divmod(total, 1440)
    hours, minutes = divmod(rest, 60)
    parts = [ar_count(n, f) for n, f in ((days, DAY_FORMS), (hours, HOUR_FORMS),
                                         (minutes, MIN_FORMS)) if n]
    return " و".join(parts) if parts else "أقل من دقيقة"


def until_label(delta: timedelta) -> str:
    minutes = math.ceil(delta.total_seconds() / 60 - 1e-9)
    return "خلال أقل من دقيقة" if minutes < 1 else "بعد " + minutes_label(minutes)


def clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'ص' if dt.hour < 12 else 'م'}"


def date_label(dt: datetime) -> str:
    return f"{WEEKDAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]}"


def offset_label(minutes: int) -> str:
    if minutes == 0:
        return "GMT"
    h, m = divmod(abs(minutes), 60)
    return f"GMT{'+' if minutes > 0 else '-'}{h}" + (f":{m:02d}" if m else "")


def tz_of(name: str | None):
    if not name:
        return UTC
    if name[0] in "+-":
        return timezone(timedelta(minutes=int(name)))
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    for _, zone, off in COUNTRIES:
        if zone == name:
            return timezone(timedelta(minutes=off))
    return UTC


def tz_label(name: str | None) -> str:
    if not name:
        return "غير محدد (GMT)"
    for label, zone, _ in COUNTRIES:
        if zone == name:
            return label
    return offset_label(int(name)) if name[0] in "+-" else name


# ------------------------------------------------------------------- alarm


class Alarm:
    def __init__(self, app):
        self.app = app
        cfg = app.content["alarm"]
        self.validate(cfg)
        self.cfg = cfg
        self.events = []
        for ev in cfg["events"]:
            h, m = (int(x) for x in ev["time_gmt"].split(":"))
            self.events.append({**ev, "h": h, "m": m})
        self.days = {DAY_KEYS.index(d) for d in (cfg.get("days") or DAY_KEYS)}
        self.lead_options = [int(n) for n in (cfg.get("lead_options") or [0, 5, 10, 15, 30, 60])]
        self.default_lead = int(cfg.get("default_lead_minutes", 10))
        self.snooze_minutes = int(cfg.get("snooze_minutes", 5))
        self._snoozes: list[tuple[datetime, int, int]] = []
        self._lock = threading.Lock()

    @staticmethod
    def validate(cfg: dict):
        events = cfg.get("events") or []
        if not cfg.get("title") or not events:
            raise ValueError("content.json: المنبه ينقصه title أو events")
        seen = set()
        for ev in events:
            for key in ("id", "name", "time_gmt"):
                if not ev.get(key):
                    raise ValueError(f"content.json: موعد في المنبه ينقصه '{key}'")
            try:
                h, m = (int(x) for x in ev["time_gmt"].split(":"))
                assert 0 <= h < 24 and 0 <= m < 60
            except Exception:
                raise ValueError(f"content.json: الوقت '{ev['time_gmt']}' غير صحيح، "
                                 "اكتبه بالشكل 03:30") from None
            if ev["id"] in seen:
                raise ValueError(f"content.json: المعرف '{ev['id']}' مكرر في المنبه")
            seen.add(ev["id"])
        for d in cfg.get("days") or []:
            if d not in DAY_KEYS:
                raise ValueError(f"content.json: اليوم '{d}' غير معروف. المتاح: {DAY_KEYS}")

    # -------------------------------------------------------- subscribers

    def _defaults(self) -> dict:
        return {"on": False, "ev": [e["id"] for e in self.events], "lead": self.default_lead,
                "tz": None, "pause": 0, "since": int(time.time())}

    def _edit(self, chat_id, **changes) -> dict:
        store = self.app.store
        with store.lock:
            sub = store.get_sub(chat_id) or self._defaults()
            sub.update(changes)
            store.set_sub(chat_id, sub)
        return sub

    def mark_blocked(self, chat_id):
        if self.app.store.get_sub(chat_id):
            self._edit(chat_id, on=False)

    def is_on(self, chat_id) -> bool:
        sub = self.app.store.get_sub(chat_id)
        return bool(sub and sub.get("on"))

    # ------------------------------------------------------------ time math

    def _at(self, ev, day) -> datetime:
        return datetime(day.year, day.month, day.day, ev["h"], ev["m"], tzinfo=UTC)

    def _occurrences(self, ev, now: datetime, before: int = 1, after: int = 1):
        base = now.astimezone(UTC).date()
        for d in range(-before, after + 1):
            t = self._at(ev, base + timedelta(days=d))
            if t.weekday() in self.days:
                yield t

    def next_alert(self, sub: dict, now: datetime):
        start = max(now, datetime.fromtimestamp(sub.get("pause") or 0, UTC))
        best = None
        for ev in self.events:
            if ev["id"] not in (sub.get("ev") or []):
                continue
            for t in self._occurrences(ev, start, before=0, after=8):
                if t > start:
                    if best is None or t < best[1]:
                        best = (ev, t)
                    break
        return best

    def _when(self, sub: dict | None, t: datetime) -> str:
        tz = (sub or {}).get("tz")
        local = t.astimezone(tz_of(tz))
        if not tz or local.utcoffset() == timedelta(0):
            return f"{clock(t)} بتوقيت غرينتش"
        return f"{clock(local)} بتوقيتك ({clock(t)} GMT)"

    def days_label(self) -> str:
        if len(self.days) == 7:
            return "يوميًا"
        if self.days == {0, 1, 2, 3, 4}:
            return "من الاثنين إلى الجمعة"
        if self.days == {6, 0, 1, 2, 3}:
            return "من الأحد إلى الخميس"
        return "، ".join(WEEKDAYS[d] for d in sorted(self.days))

    # ---------------------------------------------------------------- texts

    def main_text(self, sub, ev, target) -> str:
        return (f"🔔 {ev.get('emoji', '')} حان الآن {ev['name']}!\n\n"
                f"🕒 الساعة {self._when(sub, target)}\n\n{ev.get('message', '')}").strip()

    def pre_text(self, sub, ev, target, now) -> str:
        return (f"⏰ تذكير مسبق\n\n{ev.get('emoji', '')} {ev['name']} {until_label(target - now)}\n"
                f"🕒 الساعة {self._when(sub, target)}\n\n{ev.get('pre_message', '')}").strip()

    def snooze_text(self, sub, ev) -> str:
        return (f"🔔 تذكير: {ev.get('emoji', '')} {ev['name']}\n\n"
                f"طلبت مني أذكّرك مرة ثانية، لا تنسَ! ⏰\n\n{ev.get('message', '')}").strip()

    def alert_buttons(self, ei: int, snooze: bool = True) -> dict:
        row = [btn("✅ تمّ", "a:ok")]
        if snooze:
            row.append(btn(f"😴 ذكّرني بعد {minutes_label(self.snooze_minutes)}", f"a:sz:{ei}"))
        return kb([row])

    # ---------------------------------------------------------------- panel

    def panel(self, chat_id, now: datetime | None = None, toast: str | None = None) -> Reply:
        now = now or datetime.now(UTC)
        sub = self.app.store.get_sub(chat_id)
        title = self.cfg["title"]

        if not sub or not sub.get("on"):
            lines = [title, "", "فعّل المنبه وسأذكّرك تلقائيًا بـ:"]
            for ev in self.events:
                lines.append(f"{ev.get('emoji', '🔔')} {ev['name']}: "
                             f"{self._when(sub, self._at(ev, now.date()))}")
            lines += ["", "✨ مزايا المنبه:",
                      "• تنبيه عند الموعد بالضبط، وتذكير مسبق قبله بالمدة التي تختارها",
                      "• المواعيد تظهر بتوقيت بلدك، والتوقيت الصيفي محسوب تلقائيًا",
                      "• تختار ما يناسبك: الصفقة، أو الأرباح، أو الاثنين",
                      f"• زر «ذكّرني بعد {minutes_label(self.snooze_minutes)}» إذا كنت مشغولًا",
                      "• إيقاف مؤقت ليوم أو أكثر، ويرجع المنبه وحده بعدها"]
            if sub:
                lines += ["", "🔕 المنبه موقوف حاليًا، وإعداداتك محفوظة."]
            return Reply("\n".join(lines), kb([[btn("🔔 تفعيل المنبه", "a:on")], home_row()]),
                         toast)

        pause = sub.get("pause") or 0
        paused = pause > now.timestamp()
        if paused:
            until = datetime.fromtimestamp(pause, UTC).astimezone(tz_of(sub.get("tz")))
            status = f"⏸ متوقف مؤقتًا حتى {date_label(until)} {clock(until)}"
        else:
            status = "🟢 مفعّل"
        lines = [title, "", f"الحالة: {status}", f"🌍 توقيتك: {tz_label(sub.get('tz'))}", ""]
        for ev in self.events:
            mark = "✅" if ev["id"] in sub.get("ev", []) else "🔕"
            lines.append(f"{ev.get('emoji', '🔔')} {ev['name']}: "
                         f"{self._when(sub, self._at(ev, now.date()))} {mark}")
        lead = int(sub.get("lead") or 0)
        lines.append(f"⏳ التذكير المسبق: {'قبل ' + minutes_label(lead) if lead else 'بدون'}")
        lines.append(f"📅 الأيام: {self.days_label()}")
        nxt = self.next_alert(sub, now)
        if nxt:
            lines += ["", f"⏱ التنبيه القادم: {nxt[0]['name']} {until_label(nxt[1] - now)}"]

        ev_buttons = [btn(f"{ev.get('emoji', '')} {ev.get('button', ev['name'])} "
                          f"{'✅' if ev['id'] in sub.get('ev', []) else '❌'}", f"a:ev:{i}")
                      for i, ev in enumerate(self.events)]
        rows = [[btn("🔕 إيقاف المنبه", "a:off")], *chunk(ev_buttons, 2),
                [btn("⏳ التذكير المسبق", "a:ld"), btn("🌍 تغيير التوقيت", "a:tz")],
                [btn("▶️ استئناف التنبيهات", "a:rs") if paused else btn("⏸ إيقاف مؤقت", "a:ps")],
                [btn("🧪 جرّب التنبيه", "a:t"), btn("🔄 تحديث", "a:")],
                home_row()]
        return Reply("\n".join(lines), kb(rows), toast)

    def _tz_picker(self, sub, activate: bool) -> Reply:
        f = "1" if activate else "0"
        cur = (sub or {}).get("tz")
        rows = chunk([btn(("✅ " if zone == cur else "") + label, f"a:tz:{i}:{f}")
                      for i, (label, zone, _) in enumerate(COUNTRIES)], 3)
        rows.append([btn("🌐 بلد آخر (حسب فرق التوقيت)", f"a:ofp:{f}")])
        rows.append([btn("⬅️ رجوع", "a:")])
        text = ("🌍 اختر بلدك\n\nحتى أعرض لك المواعيد بساعتك المحلية "
                "(والتوقيت الصيفي محسوب تلقائيًا).")
        if activate:
            text += "\n\nبعد الاختيار يتفعّل المنبه مباشرة ✅"
        return Reply(text, kb(rows))

    def _offset_picker(self, sub, activate: bool) -> Reply:
        f = "1" if activate else "0"
        cur = (sub or {}).get("tz")
        rows = chunk([btn(("✅ " if cur == f"{o:+d}" else "") + offset_label(o), f"a:of:{o}:{f}")
                      for o in OFFSETS], 3)
        rows.append([btn("⬅️ رجوع", f"a:tzp:{f}")])
        return Reply("🌐 اختر فرق توقيتك عن غرينتش (GMT):", kb(rows))

    def _lead_picker(self, sub) -> Reply:
        cur = int((sub or {}).get("lead") or 0)
        opts = [btn(("✅ " if n == cur else "") + (minutes_label(n) if n else "بدون"), f"a:ld:{n}")
                for n in self.lead_options]
        return Reply("⏳ التذكير المسبق\n\nكم قبل الموعد تحب أذكّرك؟\n"
                     "التنبيه الأساسي يصلك دائمًا عند الموعد بالضبط ⏰",
                     kb(chunk(opts, 3) + [[btn("⬅️ رجوع", "a:")]]))

    def _pause_picker(self) -> Reply:
        return Reply("⏸ إيقاف التنبيهات مؤقتًا\n\nإلى متى؟ بعدها يرجع المنبه يعمل تلقائيًا.",
                     kb([[btn("يوم واحد", "a:ps:1"), btn("3 أيام", "a:ps:3"),
                          btn("أسبوع", "a:ps:7")], [btn("⬅️ رجوع", "a:")]]))

    # ------------------------------------------------------------- buttons

    def on_callback(self, ctx, parts: list[str]) -> Reply:
        act = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else None
        flag = len(parts) > 3 and parts[3] == "1"
        cid = ctx.chat_id
        sub = self.app.store.get_sub(cid)
        now = datetime.now(UTC)

        if act == "":
            return self.panel(cid, toast="🔄 تم التحديث" if len(parts) > 1 else None)
        if act == "on":
            if not sub or not sub.get("tz"):
                return self._tz_picker(sub, activate=True)
            self._edit(cid, on=True, pause=0)
            return self.panel(cid, toast="🔔 تم تفعيل المنبه")
        if act == "off":
            self._edit(cid, on=False)
            return self.panel(cid, toast="🔕 تم إيقاف المنبه")
        if act == "ev":
            ev_id = self.events[int(arg)]["id"]
            chosen = list((sub or self._defaults()).get("ev") or [])
            if ev_id in chosen:
                if len(chosen) == 1:
                    return Reply(toast="لازم يبقى تنبيه واحد على الأقل. لإيقاف كل التنبيهات "
                                       "اضغط «إيقاف المنبه».", alert=True)
                chosen.remove(ev_id)
            else:
                chosen.append(ev_id)
            self._edit(cid, ev=[e["id"] for e in self.events if e["id"] in chosen])
            return self.panel(cid, toast="✅ تم الحفظ")
        if act == "ld":
            if arg is None:
                return self._lead_picker(sub)
            self._edit(cid, lead=int(arg))
            return self.panel(cid, toast="⏳ تم ضبط التذكير المسبق")
        if act in ("tz", "of"):
            if arg is None:
                return self._tz_picker(sub, activate=False)
            zone = COUNTRIES[int(arg)][1] if act == "tz" else f"{int(arg):+d}"
            changes = {"tz": zone}
            if flag:
                changes.update(on=True, pause=0)
            self._edit(cid, **changes)
            return self.panel(cid, toast="🔔 تم تفعيل المنبه" if flag else "🌍 تم ضبط توقيتك")
        if act == "tzp":
            return self._tz_picker(sub, activate=arg == "1")
        if act == "ofp":
            return self._offset_picker(sub, activate=arg == "1")
        if act == "ps":
            if arg is None:
                return self._pause_picker()
            until = now + timedelta(days=int(arg))
            self._edit(cid, pause=int(until.timestamp()))
            return self.panel(cid, toast="⏸ تم الإيقاف المؤقت")
        if act == "rs":
            self._edit(cid, pause=0)
            return self.panel(cid, toast="▶️ رجعت التنبيهات")
        if act == "t":
            self.send_test(cid, now)
            return Reply(toast="🧪 أرسلت لك تنبيهًا تجريبيًا")
        if act == "ok":
            text = (ctx.message or {}).get("text") or ""
            return Reply(f"{text}\n\n✅ تمّ، بالتوفيق 🍀", None, toast="بالتوفيق 🍀")
        if act == "sz":
            self.snooze(cid, int(arg), now)
            later = f"😴 سأذكّرك بعد {minutes_label(self.snooze_minutes)}"
            text = (ctx.message or {}).get("text") or ""
            return Reply(f"{text}\n\n{later}", None, toast=later)
        raise ValueError(f"unknown alarm action: {act}")

    def send_test(self, chat_id, now: datetime | None = None):
        now = now or datetime.now(UTC)
        sub = self.app.store.get_sub(chat_id) or self._defaults()
        ei, ev = next(((i, e) for i, e in enumerate(self.events) if e["id"] in sub.get("ev", [])),
                      (0, self.events[0]))
        target = next(t for t in self._occurrences(ev, now, 0, 8) if t > now)
        self.app.api.call("sendMessage", chat_id=chat_id,
                          text="🧪 تنبيه تجريبي، هكذا سيصلك:\n\n" + self.main_text(sub, ev, target),
                          reply_markup=self.alert_buttons(ei))

    def snooze(self, chat_id, ei: int, now: datetime | None = None):
        self.events[ei]  # يتأكد أن الرقم صحيح
        due = (now or datetime.now(UTC)) + timedelta(minutes=self.snooze_minutes)
        with self._lock:
            self._snoozes.append((due, chat_id, ei))

    # ------------------------------------------------------------ scheduler

    def tick(self, now: datetime | None = None):
        now = now or datetime.now(UTC)
        store = self.app.store
        if not store.ready:
            return
        active = {cid: s for cid, s in store.subs().items()
                  if s.get("on") and (s.get("pause") or 0) <= now.timestamp()}
        leads = {0} | {int(s.get("lead") or 0) for s in active.values()}
        for ei, ev in enumerate(self.events):
            for target in self._occurrences(ev, now):
                for lead in sorted(leads):
                    fire_at = target - timedelta(minutes=lead)
                    if not fire_at <= now < fire_at + GRACE:
                        continue
                    if lead and target - now < timedelta(minutes=1):
                        continue  # فات وقت التذكير المسبق، سيصله التنبيه الأساسي
                    if not store.claim(f"{target:%Y-%m-%d}|{ev['id']}|{lead}", now.timestamp()):
                        continue
                    ids = [cid for cid, s in active.items() if ev["id"] in (s.get("ev") or [])
                           and (lead == 0 or int(s.get("lead") or 0) == lead)]
                    if ids:
                        self.app.run(partial(self._deliver, ids, ei, target, lead, now))
        with self._lock:
            due = [s for s in self._snoozes if s[0] <= now]
            self._snoozes = [s for s in self._snoozes if s[0] > now]
        if due:
            self.app.run(partial(self._deliver_snoozes, due))
        store.prune(now.timestamp() - 3 * 86400)
        store.flush()

    def _deliver(self, ids, ei, target, lead, now):
        ev = self.events[ei]
        store = self.app.store

        def make(cid):
            sub = store.get_sub(cid)
            if not sub or not sub.get("on"):
                return None
            if lead:
                return "sendMessage", {"text": self.pre_text(sub, ev, target, now)}
            return "sendMessage", {"text": self.main_text(sub, ev, target),
                                   "reply_markup": self.alert_buttons(ei)}

        ok, failed = send_many(self.app.api, ids, make, on_blocked=self.mark_blocked)
        print(f"alarm {ev['id']} {target:%Y-%m-%d %H:%M} lead={lead}: sent {ok}, failed {failed}",
              flush=True)

    def _deliver_snoozes(self, due):
        for _, cid, ei in due:
            sub = self.app.store.get_sub(cid) or self._defaults()
            try:
                self.app.api.call("sendMessage", chat_id=cid,
                                  text=self.snooze_text(sub, self.events[ei]),
                                  reply_markup=self.alert_buttons(ei, snooze=False))
            except TelegramError as e:
                print("snooze send failed:", e, flush=True)

    def run_forever(self, interval: int = 10):
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            time.sleep(interval)
