"""سيرفر الـ webhook (يعمل على Render): python -m bot.server

- POST /webhook   تلجرام يرسل كل رسالة هنا.
- GET  /          فحص أن السيرفر يعمل.
- GET  /setup     إعادة ربط البوت بتلجرام يدويًا (يحدث تلقائيًا عند كل تشغيل).

يحتاج متغيرًا واحدًا فقط: TELEGRAM_BOT_TOKEN.
ويبقى مستيقظًا بزيارة رابطه كل 10 دقائق (KEEP_AWAKE_MINUTES، و0 للإيقاف).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.core import TelegramAPI, TelegramError, handle_update, load_faq

FAQ = load_faq()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = TelegramAPI(TOKEN, timeout=10) if TOKEN else None
# كلمة سر الـ webhook مشتقة من التوكن، فلا حاجة لمتغير إضافي
SECRET = hashlib.sha256(f"webhook:{TOKEN}".encode()).hexdigest()[:48] if TOKEN else ""

COMMANDS = [
    {"command": "start", "description": "القائمة الرئيسية"},
    {"command": "contact", "description": "التواصل مع شخص"},
]


def public_url() -> str | None:
    url = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    return url.rstrip("/") if url else None


def ensure_webhook() -> str:
    if API is None:
        return "❌ TELEGRAM_BOT_TOKEN غير مضبوط في إعدادات السيرفر"
    base = public_url()
    if not base:
        return "❌ لم أجد رابط السيرفر (RENDER_EXTERNAL_URL أو PUBLIC_URL)"
    url = f"{base}/webhook"
    try:
        me = API.call("getMe")
        if API.call("getWebhookInfo").get("url") != url:
            API.call("setWebhook", url=url, secret_token=SECRET,
                     allowed_updates=["message", "callback_query"])
            API.call("setMyCommands", commands=COMMANDS)
        return f"✅ البوت @{me['username']} يعمل ومربوط على:\n{url}"
    except TelegramError as e:
        return f"❌ خطأ من تلجرام: {e}"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/setup":
            self._reply(200, ensure_webhook())
        else:
            self._reply(200, "bot is running")

    def do_HEAD(self):
        self._reply(200, "")

    def do_POST(self):
        if self.path.rstrip("/") != "/webhook":
            self._reply(404, "not found")
            return
        got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if API is None or not hmac.compare_digest(got, SECRET):
            self._reply(403, "forbidden")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            update = json.loads(self.rfile.read(length).decode("utf-8"))
            handle_update(update, API, FAQ)
        except Exception:
            # نرجع 200 دائمًا حتى لا يعيد تلجرام إرسال نفس الرسالة بلا نهاية
            traceback.print_exc()
        self._reply(200, "ok")

    def _reply(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        if getattr(self, "headers", None) and self.headers.get("User-Agent") == KEEP_AWAKE_UA:
            return  # لا نملأ السجل بزيارات الإبقاء مستيقظًا
        print("%s %s" % (self.address_string(), fmt % args), flush=True)


KEEP_AWAKE_UA = "keep-awake"


def keep_awake(interval: int) -> None:
    """يزور رابط السيرفر العام كل بضع دقائق حتى لا ينام في خطة Render المجانية
    (تنام الخدمة بعد 15 دقيقة بدون طلبات قادمة من الخارج)."""
    url = public_url() + "/"
    while True:
        time.sleep(interval)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": KEEP_AWAKE_UA})
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:
            print("keep-awake ping failed:", e, flush=True)


def main() -> None:
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=lambda: print(ensure_webhook(), flush=True), daemon=True).start()
    minutes = float(os.environ.get("KEEP_AWAKE_MINUTES", "10"))
    if public_url() and minutes > 0:
        threading.Thread(target=keep_awake, args=(int(minutes * 60),), daemon=True).start()
        print(f"keep-awake: ping every {minutes:g} min", flush=True)
    print(f"listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
