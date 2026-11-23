"""أدوات مشتركة لبناء الأزرار والردود."""

from __future__ import annotations

from dataclasses import dataclass

from bot.telegram import TelegramError


@dataclass
class Reply:
    text: str | None = None      # None = لا تعدّل الرسالة
    markup: dict | None = None   # None مع نص = إزالة الأزرار
    toast: str | None = None     # إشعار صغير أعلى الشاشة عند الضغط على زر
    alert: bool = False          # إظهار الإشعار كنافذة


def kb(rows) -> dict:
    return {"inline_keyboard": [r for r in rows if r]}


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def url_btn(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def home_row() -> list:
    return [btn("🏠 القائمة الرئيسية", "m")]


def chunk(items: list, n: int) -> list:
    return [items[i:i + n] for i in range(0, len(items), n)]


class Ctx:
    """محادثة واحدة: من يكتب، وأين نرد."""

    def __init__(self, app, chat_id: int, user: dict | None = None, message: dict | None = None):
        self.app = app
        self.api = app.api
        self.chat_id = chat_id
        self.user = user or {}
        self.message = message

    @property
    def is_admin(self) -> bool:
        return self.user.get("id") in self.app.admins

    def send(self, text: str, markup: dict | None = None, **extra):
        params = {"chat_id": self.chat_id, "text": text, **extra}
        if markup:
            params["reply_markup"] = markup
        return self.api.call("sendMessage", **params)

    def show(self, text: str, markup: dict | None = None):
        """يعدّل رسالة الزر نفسها إن أمكن (قائمة تتنقل في مكانها)، وإلا يرسل رسالة جديدة."""
        m = self.message
        if m and m.get("text") is not None:
            params = {"chat_id": self.chat_id, "message_id": m["message_id"], "text": text}
            if markup:
                params["reply_markup"] = markup
            try:
                self.api.call("editMessageText", **params)
                return
            except TelegramError as e:
                if "not modified" in str(e):
                    return
        self.send(text, markup)
