"""تشغيل البوت على جهازك للتجربة (long polling).

يحتاج اتصالًا يصل لتلجرام (VPN على مستوى الجهاز كله، وليس إضافة متصفح فقط).

    python run_local.py          تشغيل البوت
    python run_local.py --check  التأكد من التوكن والاتصال فقط
"""

import os
import re
import sys
import threading
import time

from config import load_env

load_env()

from bot.core import (ALLOWED_UPDATES, App, TelegramAPI, TelegramError,  # noqa: E402
                      handle_update, load_content)
from bot.storage import Store  # noqa: E402


def main() -> None:
    api = TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    admins = [int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", ""))
              if x.lstrip("-").isdigit()]
    store = Store(local_path="state.json")
    store.load()
    app = App(api, load_content(), store, admins)
    me = api.call("getMe")
    app.me = me
    print(f"متصل كبوت: @{me['username']}")
    if "--check" in sys.argv:
        return

    hook = api.call("getWebhookInfo")
    if hook.get("url"):
        print("⚠️ البوت مربوط حاليًا بـ webhook على السيرفر:", hook["url"])
        print("التشغيل المحلي سيفصله. افتح رابط /setup على السيرفر بعدها لإعادة ربطه.")
        if "--force" not in sys.argv:
            print("أضف --force إذا كنت متأكدًا.")
            return
        api.call("deleteWebhook")

    threading.Thread(target=store.run_saver, daemon=True).start()
    threading.Thread(target=app.alarm.run_forever, daemon=True).start()
    print("البوت يعمل… (Ctrl+C للإيقاف)")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ALLOWED_UPDATES}
            if offset is not None:
                params["offset"] = offset
            for update in api.call("getUpdates", **params):
                offset = update["update_id"] + 1
                try:
                    handle_update(update, app)
                except Exception as e:
                    print("خطأ أثناء الرد:", e)
        except KeyboardInterrupt:
            store.flush()
            print("\nتم الإيقاف.")
            return
        except (TelegramError, OSError) as e:
            print("مشكلة اتصال، إعادة المحاولة بعد 5 ثوانٍ:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
