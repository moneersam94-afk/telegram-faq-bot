"""سيرفر الـ webhook (يعمل على Render): python -m bot.server

- POST /webhook   تلجرام يرسل كل رسالة هنا.
- GET  /          فحص أن السيرفر يعمل.
- GET  /setup     إعادة ربط البوت بتلجرام يدويًا (يحدث تلقائيًا عند كل تشغيل).

متغيرات البيئة:
- TELEGRAM_BOT_TOKEN  (إجباري) التوكن من BotFather.
- ADMIN_IDS           معرفات المدراء مفصولة بفواصل (اعرف معرّفك بالأمر /myid).
- STORAGE_CHAT_ID     (اختياري) محادثة حفظ البيانات. الافتراضي: محادثة أول مدير مع البوت.
- KEEP_AWAKE_MINUTES  مدة زيارة السيرفر لنفسه حتى لا ينام (الافتراضي 10، و0 للإيقاف).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.core import ALLOWED_UPDATES, App, TelegramAPI, TelegramError, handle_update, load_content
from bot.storage import Store

CONTENT = load_content()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = TelegramAPI(TOKEN, timeout=15) if TOKEN else None
# كلمة سر الـ webhook مشتقة من التوكن، فلا حاجة لمتغير إضافي
SECRET = hashlib.sha256(f"webhook:{TOKEN}".encode()).hexdigest()[:48] if TOKEN else ""

ADMINS = [int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", ""))
          if x.lstrip("-").isdigit()]
_storage = os.environ.get("STORAGE_CHAT_ID", "").strip() or (str(ADMINS[0]) if ADMINS else "")
STORAGE_CHAT = int(_storage) if _storage.lstrip("-").isdigit() else (_storage or None)
STORE = Store(api=API if STORAGE_CHAT else None, chat_id=STORAGE_CHAT,
              local_path=os.environ.get("STATE_FILE", "state.json"))
APP = App(API, CONTENT, STORE, ADMINS) if API else None

COMMANDS = [
    {"command": "start", "description": "القائمة الرئيسية"},
    {"command": "alarm", "description": "منبه الصفقات اليومية"},
    {"command": "faq", "description": "الأسئلة الشائعة"},
    {"command": "report", "description": "الإبلاغ عن مشكلة"},
]


def public_url() -> str | None:
    url = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    return url.rstrip("/") if url else None


def storage_status() -> str:
    if not STORE.remote:
        return "⚠️ مؤقت (أضف ADMIN_IDS ليصبح دائمًا)"
    return "دائم على تلجرام ✅" if STORE.ready else "⏳ جارٍ التحميل…"


def ensure_webhook() -> str:
    if API is None:
        return "❌ TELEGRAM_BOT_TOKEN غير مضبوط في إعدادات السيرفر"
    base = public_url()
    if not base:
        return "❌ لم أجد رابط السيرفر (RENDER_EXTERNAL_URL أو PUBLIC_URL)"
    url = f"{base}/webhook"
    try:
        me = API.call("getMe")
        info = API.call("getWebhookInfo")
        if info.get("url") != url or set(info.get("allowed_updates") or []) != set(ALLOWED_UPDATES):
            API.call("setWebhook", url=url, secret_token=SECRET, allowed_updates=ALLOWED_UPDATES)
        API.call("setMyCommands", commands=COMMANDS)
        return (f"✅ البوت @{me['username']} يعمل ومربوط على:\n{url}\n"
                f"💾 التخزين: {storage_status()}\n👑 عدد المدراء: {len(ADMINS)}")
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
        if APP is None or not hmac.compare_digest(got, SECRET):
            self._reply(403, "forbidden")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            update = json.loads(self.rfile.read(length).decode("utf-8"))
            handle_update(update, APP)
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


def _shutdown(signum, frame):
    print("shutting down, saving state…", flush=True)
    STORE.flush()
    os._exit(0)


def main() -> None:
    port = int(os.environ.get("PORT", "10000"))
    if APP:
        for _ in range(3):
            if STORE.load():
                break
            time.sleep(2)
        print(f"storage: {'telegram chat' if STORE.remote else 'local file'}, "
              f"{'ready' if STORE.ready else 'NOT ready, retrying in background'}, "
              f"subscribers: {len(STORE.subs())}", flush=True)
        threading.Thread(target=STORE.run_saver, daemon=True).start()
        threading.Thread(target=APP.alarm.run_forever, daemon=True).start()
        signal.signal(signal.SIGTERM, _shutdown)
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
