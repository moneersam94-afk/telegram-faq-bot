"""منطق البوت: قراءة الأسئلة والأجوبة، البحث فيها، والرد على تحديثات تلجرام.

لا يعتمد على أي مكتبة خارجية (مكتبة بايثون القياسية فقط)،
ويُستخدم من وضع التشغيل المحلي (run_local.py) ومن الـ webhook على Vercel.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

FAQ_PATH = Path(__file__).with_name("faq.json")
API_BASE = "https://api.telegram.org"


# ---------------------------------------------------------------- Telegram API


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str, timeout: int = 40):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN غير موجود")
        self._url = f"{API_BASE}/bot{token}/"
        self.timeout = timeout

    def call(self, method: str, **params):
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            self._url + method,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                raise TelegramError(f"{method}: HTTP {e.code}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body.get("result")


# ------------------------------------------------------------------- FAQ data


def load_faq(path: Path = FAQ_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        faq = json.load(f)
    for key in ("company_name", "welcome", "fallback", "categories"):
        if key not in faq:
            raise ValueError(f"faq.json: الحقل '{key}' مفقود")
    if not faq["categories"]:
        raise ValueError("faq.json: لا توجد أقسام")
    for ci, cat in enumerate(faq["categories"]):
        if not cat.get("title") or not cat.get("questions"):
            raise ValueError(f"faq.json: القسم رقم {ci + 1} بلا عنوان أو بلا أسئلة")
        for qi, item in enumerate(cat["questions"]):
            if not item.get("q") or not item.get("a"):
                raise ValueError(
                    f"faq.json: السؤال {qi + 1} في قسم '{cat['title']}' ينقصه q أو a"
                )
    return faq


def fill(text: str, faq: dict) -> str:
    return text.replace("{company}", faq["company_name"])


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


def search(faq: dict, text: str, limit: int = 3) -> list[tuple[int, int, int]]:
    """يرجع قائمة (الدرجة، رقم القسم، رقم السؤال) مرتبة من الأقرب."""
    norm = f" {normalize(text)} "
    query = tokens(text)
    results = []
    for ci, cat in enumerate(faq["categories"]):
        for qi, item in enumerate(cat["questions"]):
            score = 0
            for kw in item.get("keywords", []):
                nkw = normalize(kw)
                if not nkw:
                    continue
                if " " in nkw:  # عبارة من أكثر من كلمة
                    hit = f" {nkw} " in norm
                else:  # كلمة واحدة: تطابق أيضًا مع اللواحق مثل "موقعكم" ← "موقع"
                    kw_stem = _stem(nkw)
                    hit = kw_stem in query or (
                        len(kw_stem) >= 3 and any(t.startswith(kw_stem) for t in query)
                    )
                if hit:
                    score += 3
            score += len(query & tokens(item["q"]))
            if score:
                results.append((score, ci, qi))
    results.sort(key=lambda r: -r[0])
    return results[:limit]


MATCH_THRESHOLD = 2


# -------------------------------------------------------------- Keyboards


def _kb(rows):
    return {"inline_keyboard": rows}


def main_menu(faq: dict):
    rows = [
        [{"text": cat["title"], "callback_data": f"c:{ci}"}]
        for ci, cat in enumerate(faq["categories"])
    ]
    if faq.get("human_contact"):
        rows.append([{"text": "🙋 تواصل مع شخص", "callback_data": "h"}])
    return _kb(rows)


def category_menu(faq: dict, ci: int):
    cat = faq["categories"][ci]
    rows = [
        [{"text": item["q"], "callback_data": f"q:{ci}:{qi}"}]
        for qi, item in enumerate(cat["questions"])
    ]
    rows.append([{"text": "⬅️ القائمة الرئيسية", "callback_data": "m"}])
    return _kb(rows)


def answer_menu(ci: int):
    return _kb(
        [
            [{"text": "⬅️ رجوع للقسم", "callback_data": f"c:{ci}"}],
            [{"text": "🏠 القائمة الرئيسية", "callback_data": "m"}],
        ]
    )


def back_menu():
    return _kb([[{"text": "🏠 القائمة الرئيسية", "callback_data": "m"}]])


# --------------------------------------------------------------- Handlers


def welcome_text(faq: dict) -> str:
    parts = [fill(faq["welcome"], faq)]
    if faq.get("disclaimer"):
        parts.append("ℹ️ " + fill(faq["disclaimer"], faq))
    parts.append("اختر من القائمة، أو اكتب سؤالك مباشرة 👇")
    return "\n\n".join(parts)


def human_text(faq: dict) -> str:
    return fill(faq["human_contact"], faq)


def answer_text(faq: dict, ci: int, qi: int) -> str:
    item = faq["categories"][ci]["questions"][qi]
    return f"❓ {item['q']}\n\n{fill(item['a'], faq)}"


def handle_update(update: dict, api: TelegramAPI, faq: dict) -> None:
    if "callback_query" in update:
        _handle_callback(update["callback_query"], api, faq)
        return

    msg = update.get("message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return  # يرد فقط في المحادثات الخاصة
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if not text:
        api.call("sendMessage", chat_id=chat_id,
                 text="أستقبل الرسائل النصية فقط حاليًا. اكتب سؤالك أو اختر من القائمة.",
                 reply_markup=main_menu(faq))
        return

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    if command in ("/start", "/menu", "/help"):
        api.call("sendMessage", chat_id=chat_id, text=welcome_text(faq),
                 reply_markup=main_menu(faq))
        return
    if command == "/contact" and faq.get("human_contact"):
        api.call("sendMessage", chat_id=chat_id, text=human_text(faq),
                 reply_markup=back_menu())
        return

    matches = [m for m in search(faq, text) if m[0] >= MATCH_THRESHOLD]
    if not matches:
        api.call("sendMessage", chat_id=chat_id, text=fill(faq["fallback"], faq),
                 reply_markup=main_menu(faq))
        return

    best_score, ci, qi = matches[0]
    others = [m for m in matches[1:] if m[0] >= best_score - 1 and m[1:] != (ci, qi)]
    rows = [[{"text": "🔎 " + faq["categories"][c]["questions"][q]["q"],
              "callback_data": f"q:{c}:{q}"}] for _, c, q in others]
    rows.append([{"text": "🏠 القائمة الرئيسية", "callback_data": "m"}])
    text_out = answer_text(faq, ci, qi)
    if others:
        text_out += "\n\nقد يهمك أيضًا:"
    api.call("sendMessage", chat_id=chat_id, text=text_out, reply_markup=_kb(rows))


def _handle_callback(cq: dict, api: TelegramAPI, faq: dict) -> None:
    data = cq.get("data", "")
    message = cq.get("message")
    try:
        api.call("answerCallbackQuery", callback_query_id=cq["id"])
    except TelegramError:
        pass
    if not message:
        return

    text, markup = None, None
    parts = data.split(":")
    try:
        if data == "m":
            text, markup = welcome_text(faq), main_menu(faq)
        elif data == "h" and faq.get("human_contact"):
            text, markup = human_text(faq), back_menu()
        elif parts[0] == "c" and len(parts) == 2:
            ci = int(parts[1])
            cat = faq["categories"][ci]
            text, markup = f"📂 {cat['title']}\n\nاختر سؤالك:", category_menu(faq, ci)
        elif parts[0] == "q" and len(parts) == 3:
            ci, qi = int(parts[1]), int(parts[2])
            text, markup = answer_text(faq, ci, qi), answer_menu(ci)
    except (ValueError, IndexError):
        text = None  # زر قديم بعد تعديل faq.json

    if text is None:
        text, markup = "هذا الزر لم يعد متاحًا. هذه القائمة المحدّثة:", main_menu(faq)

    try:
        api.call("editMessageText", chat_id=message["chat"]["id"],
                 message_id=message["message_id"], text=text, reply_markup=markup)
    except TelegramError as e:
        if "not modified" not in str(e):
            api.call("sendMessage", chat_id=message["chat"]["id"], text=text,
                     reply_markup=markup)
