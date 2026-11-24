"""منطق البوت: القوائم، الشروحات، الأسئلة الشائعة، الإبلاغ عن مشكلة، والرد على تلجرام.

لا يعتمد على أي مكتبة خارجية. كل النصوص في content.json.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from bot import admin
from bot.alarm import Alarm
from bot.groups import GROUP_TYPES, Groups
from bot.storage import Store
from bot.telegram import TelegramAPI, TelegramError  # noqa: F401  (يُستورد من هنا أيضًا)
from bot.ui import Ctx, Reply, btn, home_row, kb, url_btn

CONTENT_PATH = Path(__file__).with_name("content.json")
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]


# ------------------------------------------------------------------ content


def load_content(path: Path = CONTENT_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    for key in ("welcome", "prompt", "fallback", "explanations", "faq", "report", "alarm"):
        if key not in c:
            raise ValueError(f"content.json: الحقل '{key}' مفقود")
    for key in ("explanations", "faq", "report"):
        if not c[key].get("title"):
            raise ValueError(f"content.json: القسم '{key}' بلا title")
    for i, t in enumerate(c["explanations"].get("topics") or []):
        if not t.get("title") or not (t.get("text") or topic_posts(t)):
            raise ValueError(f"content.json: الشرح رقم {i + 1} ينقصه title أو text/posts")
        for b in t.get("buttons") or []:
            if not b.get("text") or not b.get("url"):
                raise ValueError(f"content.json: زر في الشرح '{t['title']}' ينقصه text أو url")
    for ci, cat in enumerate(c["faq"].get("categories") or []):
        if not cat.get("title") or not cat.get("questions"):
            raise ValueError(f"content.json: قسم الأسئلة رقم {ci + 1} بلا عنوان أو بلا أسئلة")
        for qi, item in enumerate(cat["questions"]):
            if not item.get("q") or not item.get("a"):
                raise ValueError(f"content.json: السؤال {qi + 1} في '{cat['title']}' ينقصه q أو a")
    for i, t in enumerate(c["report"].get("types") or []):
        if not t.get("title"):
            raise ValueError(f"content.json: نوع المشكلة رقم {i + 1} بلا title")
    acc = c.get("accounts")
    if acc is not None:
        if not acc.get("title"):
            raise ValueError("content.json: الحسابات المعتمدة بلا title")
        for row in acc.get("buttons") or []:
            for b in row:
                if not b.get("text") or not b.get("url"):
                    raise ValueError("content.json: زر في الحسابات المعتمدة ينقصه text أو url")
    Alarm.validate(c["alarm"])
    return c


class App:
    def __init__(self, api, content: dict, store: Store | None = None, admins=(), run=None):
        self.api = api
        self.content = content
        self.store = store if store is not None else Store()
        self.admins = {int(a) for a in admins}
        self.run = run or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.me: dict = {}       # بيانات البوت نفسه من getMe (اليوزر والمعرّف)
        self.pending: dict = {}  # عمليات تنتظر رسالة: مدير يراسل مستخدمًا، أو مستخدم يرد
        self.alarm = Alarm(self)
        self.groups = Groups(self)


# ------------------------------------------------------- Arabic text matching

_DIACRITICS = re.compile(r"[ً-ْٰـ]")
_NON_WORD = re.compile(r"[^\w\s]")
_PREFIXES = ("وال", "بال", "فال", "كال", "لل", "ال")
_STOPWORDS = {
    "في", "من", "الى", "على", "عن", "مع", "هل", "ما", "ماذا", "متى", "كيف", "كم",
    "اين", "وين", "ليش", "لماذا", "هو", "هي", "انا", "انت", "عندكم", "عندك",
    "يا", "او", "ثم", "لو", "اذا", "اللي", "الذي", "التي", "شو", "ايش", "وش",
    "ابي", "ابغى", "اريد", "ممكن", "لكم", "لي", "فيه", "هذا", "هذه", "ذلك",
}


def normalize(text: str) -> str:
    text = _DIACRITICS.sub("", text.lower())
    text = re.sub("[أإآٱ]", "ا", text)
    text = text.replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


_STOPWORDS = {normalize(w) for w in _STOPWORDS}


def _stem(word: str) -> str:
    for p in _PREFIXES:
        if word.startswith(p) and len(word) - len(p) >= 2:
            return word[len(p):]
    return word


def tokens(text: str) -> set[str]:
    words = normalize(text).split()
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1}


def _keyword_hit(kw: str, norm: str, query: set[str]) -> bool:
    nkw = normalize(kw)
    if not nkw:
        return False
    if " " in nkw:  # عبارة من أكثر من كلمة
        return f" {nkw} " in norm
    kw_stem = _stem(nkw)  # كلمة واحدة: تطابق أيضًا مع اللواحق مثل "موقعكم" ← "موقع"
    return kw_stem in query or (len(kw_stem) >= 3 and any(t.startswith(kw_stem) for t in query))


def search(categories: list, text: str, limit: int = 3) -> list[tuple[int, int, int]]:
    """يرجع قائمة (الدرجة، رقم القسم، رقم السؤال) مرتبة من الأقرب."""
    norm = f" {normalize(text)} "
    query = tokens(text)
    results = []
    for ci, cat in enumerate(categories):
        for qi, item in enumerate(cat["questions"]):
            score = 3 * sum(_keyword_hit(kw, norm, query) for kw in item.get("keywords", []))
            score += len(query & tokens(item["q"]))
            if score:
                results.append((score, ci, qi))
    results.sort(key=lambda r: -r[0])
    return results[:limit]


MATCH_THRESHOLD = 2

# كلمات تفتح القسم المناسب إذا لم يوجد جواب في الأسئلة الشائعة (بالترتيب)
INTENTS = [
    ("report", ["مشكله", "مشاكل", "عطل", "خطا", "بلاغ", "ابلاغ", "شكوي", "شكوه"]),
    ("explain", ["شرح", "اشرح", "فيديو", "تعلم", "علمني"]),
    ("alarm", ["منبه", "تنبيه", "تذكير", "ذكرني", "صفقه", "صفقات", "ارباح", "موعد"]),
]


def detect_intent(text: str) -> str | None:
    norm, query = f" {normalize(text)} ", tokens(text)
    for name, words in INTENTS:
        if any(_keyword_hit(w, norm, query) for w in words):
            return name
    return None


# -------------------------------------------------------------------- menus


def welcome_text(c: dict, user: dict | None = None) -> str:
    name = ((user or {}).get("first_name") or "").strip()
    w = c["welcome"].replace("{name}", name) if name else \
        c["welcome"].replace(" {name}", "").replace("{name}", "")
    parts = [w]
    if c.get("disclaimer"):
        parts.append("ℹ️ " + c["disclaimer"])
    parts.append(c["prompt"])
    return "\n\n".join(parts)


def main_menu(app: App, chat_id) -> dict:
    c = app.content
    alarm_label = ("⏰ منبه الصفقات اليومية (مفعّل ✅)" if app.alarm.is_on(chat_id)
                   else c["alarm"].get("button") or c["alarm"]["title"])
    second = [btn(c["report"]["title"], "r")]
    if c.get("accounts"):
        second.insert(0, btn(c["accounts"]["title"], "acc"))
    return kb([[btn(alarm_label, "a")],
               [btn(c["explanations"]["title"], "e"), btn(c["faq"]["title"], "f")],
               second])


def accounts_reply(c: dict) -> Reply:
    acc = c["accounts"]
    text = "\n\n".join(x for x in (acc["title"], acc.get("text"), acc.get("warning")) if x)
    rows = [[url_btn(b["text"], b["url"]) for b in row] for row in acc.get("buttons") or []]
    return Reply(text, kb(rows + [home_row()]))


def home_reply(ctx: Ctx) -> Reply:
    return Reply(welcome_text(ctx.app.content, ctx.user), main_menu(ctx.app, ctx.chat_id))


def _section_text(sec: dict) -> str:
    return f"{sec['title']}\n\n{sec.get('intro', '')}".strip()


# ----------------------------------------------------------- explanations

MEDIA = {"video": "sendVideo", "photo": "sendPhoto", "document": "sendDocument",
         "animation": "sendAnimation", "audio": "sendAudio", "voice": "sendVoice"}
_POST_LINK = re.compile(r"^(?:https?://)?t\.me/(c/)?([A-Za-z0-9_]+)/(\d+)/?$")


def media_call(src: str) -> tuple[str, dict]:
    """رابط منشور في قناة ← نسخه (بدون "محوّل من")، أو معرف ملف ← إرساله مباشرة."""
    src = src.strip()
    m = _POST_LINK.match(src)
    if m:
        chat = int("-100" + m.group(2)) if m.group(1) else "@" + m.group(2)
        return "copyMessage", {"from_chat_id": chat, "message_id": int(m.group(3))}
    kind, sep, file_id = src.partition(":")
    if sep and kind in MEDIA:
        return MEDIA[kind], {kind: file_id}
    return "sendVideo", {"video": src}


def topic_posts(t: dict) -> list:
    """روابط منشورات القناة أو معرفات الملفات ("posts"، والاسم القديم "videos")."""
    return list(t.get("posts") or []) + list(t.get("videos") or [])


def explain_menu(c: dict) -> Reply:
    ex = c["explanations"]
    topics = ex.get("topics") or []
    if not topics:
        return Reply(ex.get("empty") or f"{ex['title']}\n\nقريبًا ⏳", kb([home_row()]))
    rows = [[btn(t["title"], f"e:{i}")] for i, t in enumerate(topics)]
    return Reply(_section_text(ex), kb(rows + [home_row()]))


def topic_markup(t: dict) -> dict:
    rows = [[url_btn(b["text"], b["url"])] for b in t.get("buttons") or []]
    rows.append([btn("⬅️ الشروحات", "e"), btn("🏠 الرئيسية", "m")])
    return kb(rows)


def show_topic(ctx: Ctx, i: int) -> Reply:
    t = ctx.app.content["explanations"]["topics"][i]
    text = f"{t['title']}\n\n{t.get('text', '')}".strip()
    markup = topic_markup(t)
    videos = topic_posts(t)
    if not videos:
        return Reply(text, markup)

    single = len(videos) == 1 and len(text) <= 1024  # حد وصف الفيديو في تلجرام
    if not single:
        ctx.send(text)
    failed, last_ok = [], True
    for n, src in enumerate(videos):
        last = n == len(videos) - 1
        method, params = media_call(src)
        # منشور القناة بلا نص في الشرح: نترك وصفه الأصلي كما هو
        if single and (t.get("text") or method != "copyMessage"):
            params["caption"] = text
        if last:
            params["reply_markup"] = markup
        try:
            ctx.api.call(method, chat_id=ctx.chat_id, **params)
        except TelegramError as e:
            print(f"explain media failed ({src}): {e}", flush=True)
            failed.append(src)
            last_ok = not last
    if failed:
        # احتياط: زر يفتح المنشور في القناة مباشرة حتى لا يبقى الشرح فارغًا
        links = [s if s.startswith("http") else "https://" + s for s in failed if _POST_LINK.match(s)]
        rows = [[url_btn("▶️ مشاهدة الشرح في القناة", u)] for u in links]
        if not last_ok:
            rows += markup["inline_keyboard"]
        note = ("تقدر تشاهده من القناة مباشرة 👇" if links else "حاول مرة أخرى لاحقًا.")
        ctx.send((text + "\n\n" if single else "") + f"⚠️ تعذّر عرض الفيديو هنا. {note}", kb(rows))
    return Reply(toast="🎬 تفضّل الشرح")


# --------------------------------------------------------------------- FAQ


def faq_menu(c: dict) -> Reply:
    faq = c["faq"]
    cats = faq.get("categories") or []
    if not cats:
        return Reply(faq.get("empty") or f"{faq['title']}\n\nقريبًا ⏳", kb([home_row()]))
    rows = [[btn(cat["title"], f"c:{ci}")] for ci, cat in enumerate(cats)]
    return Reply(_section_text(faq), kb(rows + [home_row()]))


def category_reply(c: dict, ci: int) -> Reply:
    cat = c["faq"]["categories"][ci]
    rows = [[btn(item["q"], f"q:{ci}:{qi}")] for qi, item in enumerate(cat["questions"])]
    rows.append([btn("⬅️ الأقسام", "f"), btn("🏠 الرئيسية", "m")])
    return Reply(f"📂 {cat['title']}\n\nاختر سؤالك:", kb(rows))


def answer_reply(c: dict, ci: int, qi: int) -> Reply:
    item = c["faq"]["categories"][ci]["questions"][qi]
    return Reply(f"❓ {item['q']}\n\n{item['a']}",
                 kb([[btn("⬅️ رجوع للقسم", f"c:{ci}")], home_row()]))


# ------------------------------------------------------------------ report


def contact_url(contact: str) -> str | None:
    contact = contact.strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{4,32}", contact):
        return "https://t.me/" + contact[1:]
    if re.match(r"^(https?://)?t\.me/\S+$", contact):
        return contact if contact.startswith("http") else "https://" + contact
    return None


def report_menu(c: dict) -> Reply:
    r = c["report"]
    rows = [[btn(t["title"], f"r:{i}")] for i, t in enumerate(r.get("types") or [])]
    return Reply(_section_text(r), kb(rows + [home_row()]))


def report_type(c: dict, i: int) -> Reply:
    r = c["report"]
    t = r["types"][i]
    contact = (t.get("contact") or "").strip()
    url = contact_url(contact) if contact else None
    lines = [t["title"], ""]
    if url:
        lines += [f"👤 المختص: {contact}", "اضغط الزر بالأسفل لمراسلته مباشرة 👇"]
    elif contact:
        lines += [f"👤 للتواصل: {contact}"]
    else:
        lines += [r.get("no_contact") or "⏳ سيتم إضافة وسيلة التواصل لهذا النوع قريبًا."]
    hints = t.get("hints") or r.get("hints") or []
    if hints:
        lines += ["", r.get("hints_title") or "📝 حتى تُحل مشكلتك أسرع، أرسل له:"]
        lines += [f"• {h}" for h in hints]
    rows = [[url_btn("💬 مراسلة المختص", url)]] if url else []
    rows.append([btn("⬅️ أنواع المشاكل", "r"), btn("🏠 الرئيسية", "m")])
    return Reply("\n".join(lines), kb(rows))


# ---------------------------------------------------------------- handlers


def handle_update(update: dict, app: App) -> None:
    if "callback_query" in update:
        _on_callback(update["callback_query"], app)
    elif "my_chat_member" in update:
        upd = update["my_chat_member"]
        if (upd.get("chat") or {}).get("type") in GROUP_TYPES:
            app.groups.on_member(upd)
        else:
            _on_member(upd, app)
    elif update.get("message"):
        msg = update["message"]
        chat_type = (msg.get("chat") or {}).get("type")
        if chat_type == "private":
            _on_message(msg, app)  # يرد على الأسئلة في المحادثات الخاصة فقط
        elif chat_type in GROUP_TYPES:
            app.groups.on_group_message(msg)


def _on_message(msg: dict, app: App) -> None:
    ctx = Ctx(app, msg["chat"]["id"], msg.get("from"))
    app.store.touch_user(ctx.chat_id, msg.get("from"))
    text = (msg.get("text") or "").strip()

    state = app.pending.get(ctx.chat_id)
    if text.split("@")[0].lower() == "/cancel":
        app.pending.pop(ctx.chat_id, None)
        ctx.send("✖️ تم الإلغاء." if state else "لا توجد عملية لإلغائها.",
                 main_menu(app, ctx.chat_id))
        return
    if state:
        if text.startswith("/"):
            app.pending.pop(ctx.chat_id, None)  # أي أمر آخر يلغي العملية الجارية
        else:
            app.pending.pop(ctx.chat_id, None)
            if state["act"] == "send" and ctx.is_admin:
                admin.confirm_send(ctx, msg, state["to"])
            elif state["act"] == "reply":
                admin.forward_reply(ctx, msg)
            return

    if ctx.is_admin and admin.on_media(ctx, msg):
        return
    if not text:
        ctx.send("أستقبل الرسائل النصية فقط حاليًا. اختر من القائمة أو اكتب سؤالك 👇",
                 main_menu(app, ctx.chat_id))
        return
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        reply = _command(ctx, cmd.split("@")[0].lower(), arg.strip(), msg)
    else:
        reply = _answer_text(ctx, text)
    if reply:
        ctx.send(reply.text, reply.markup)


def _command(ctx: Ctx, cmd: str, arg: str, msg: dict):
    c = ctx.app.content
    if cmd == "/start" and arg:  # روابط مباشرة مثل t.me/البوت?start=alarm
        cmd = "/" + arg.lower()
    if cmd == "/alarm":
        return ctx.app.alarm.panel(ctx.chat_id)
    if cmd == "/faq":
        return faq_menu(c)
    if cmd == "/report":
        return report_menu(c)
    if cmd in ("/explain", "/learn"):
        return explain_menu(c)
    if cmd == "/myid":
        return Reply(f"🆔 معرّفك في تلجرام: {ctx.user.get('id')}")
    if ctx.is_admin:
        result = admin.on_command(ctx, cmd, arg, msg)
        if result is not False:
            return result
    return home_reply(ctx)


def _answer_text(ctx: Ctx, text: str) -> Reply:
    c = ctx.app.content
    cats = c["faq"].get("categories") or []
    matches = [m for m in search(cats, text) if m[0] >= MATCH_THRESHOLD]
    if matches:
        best_score, ci, qi = matches[0]
        others = [m for m in matches[1:] if m[0] >= best_score - 1 and m[1:] != (ci, qi)]
        rows = [[btn("🔎 " + cats[oc]["questions"][oq]["q"], f"q:{oc}:{oq}")]
                for _, oc, oq in others]
        item = cats[ci]["questions"][qi]
        out = f"❓ {item['q']}\n\n{item['a']}" + ("\n\nقد يهمك أيضًا:" if others else "")
        return Reply(out, kb(rows + [home_row()]))
    intent = detect_intent(text)
    if intent == "report":
        return report_menu(c)
    if intent == "explain":
        return explain_menu(c)
    if intent == "alarm":
        return ctx.app.alarm.panel(ctx.chat_id)
    return Reply(c["fallback"], main_menu(ctx.app, ctx.chat_id))


def _route(ctx: Ctx, data: str) -> Reply:
    c = ctx.app.content
    parts = data.split(":")
    head = parts[0]
    if data == "m":
        return home_reply(ctx)
    if data == "e":
        return explain_menu(c)
    if head == "e":
        return show_topic(ctx, int(parts[1]))
    if data == "f":
        return faq_menu(c)
    if head == "c":
        return category_reply(c, int(parts[1]))
    if head == "q":
        return answer_reply(c, int(parts[1]), int(parts[2]))
    if data in ("r", "h"):
        return report_menu(c)
    if data == "acc" and c.get("accounts"):
        return accounts_reply(c)
    if head == "gr":
        return ctx.app.groups.on_callback(ctx, parts)
    if data == "rp":  # المستخدم يريد الرد على رسالة الإدارة
        ctx.app.pending[ctx.chat_id] = {"act": "reply"}
        ctx.send("✍️ اكتب ردك الآن (نص أو صورة أو فيديو) وسأوصله للإدارة.\nللإلغاء: /cancel")
        return Reply(toast="✍️ اكتب ردك")
    if head == "r":
        return report_type(c, int(parts[1]))
    if head == "a":
        return ctx.app.alarm.on_callback(ctx, parts)
    if head == "ad":
        return admin.on_callback(ctx, parts)
    raise ValueError(data)


def _on_callback(cq: dict, app: App) -> None:
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    reply = Reply()
    if chat.get("type") == "private":
        ctx = Ctx(app, chat["id"], cq.get("from"), msg)
        try:
            reply = _route(ctx, cq.get("data") or "")
        except (ValueError, IndexError, KeyError, TypeError):
            reply = Reply("هذا الزر لم يعد متاحًا. هذه القائمة المحدّثة 👇",
                          main_menu(app, ctx.chat_id))
    params = {"callback_query_id": cq["id"]}
    if reply.toast:
        params.update(text=reply.toast, show_alert=reply.alert)
    try:
        app.api.call("answerCallbackQuery", **params)
    except TelegramError:
        pass
    if chat.get("type") == "private" and reply.text is not None:
        ctx.show(reply.text, reply.markup)


def _on_member(upd: dict, app: App) -> None:
    """عند إضافة البوت مشرفًا في قناة، يُبلغ المدراء في الخاص (ولا يكتب شيئًا في القناة)."""
    chat = upd.get("chat") or {}
    status = (upd.get("new_chat_member") or {}).get("status")
    if chat.get("type") == "channel" and status == "administrator":
        name = chat.get("title", "")
        if chat.get("username"):
            name += f" (@{chat['username']})"
        for admin_id in app.admins:
            try:
                app.api.call("sendMessage", chat_id=admin_id,
                             text=f"✅ تمت إضافتي مشرفًا في القناة: {name}\n🆔 معرّفها: {chat['id']}\n\n"
                                  "صار بإمكاني عرض منشوراتها في الشروحات.")
            except TelegramError:
                pass
