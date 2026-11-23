"""حفظ بيانات البوت (مشتركو المنبه وإعداداتهم) بشكل دائم.

خوادم Render المجانية تمسح الملفات عند كل نشر أو إعادة تشغيل، لذلك تُحفظ البيانات
كملف JSON مثبّت في محادثة على تلجرام نفسه (محادثة المدير مع البوت أو قناة خاصة).
بدون محادثة تخزين تُحفظ في ملف محلي (مناسب للتجربة فقط).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from bot.telegram import TelegramError

FILE_NAME = "wealth_bot_state.json"
CAPTION = ("🗄 نسخة بيانات البوت (مشتركو المنبه وإعداداتهم).\n"
           "تتحدث تلقائيًا. لا تحذف هذه الرسالة ولا تلغِ تثبيتها.")
SECTIONS = ("subs", "users", "fired")


class Store:
    def __init__(self, api=None, chat_id=None, local_path=None):
        self.api = api
        self.chat_id = chat_id
        self.local_path = Path(local_path) if local_path else None
        self.lock = threading.RLock()
        self._write_lock = threading.Lock()
        self.data = {k: {} for k in SECTIONS}
        self.ready = False  # لا نحفظ شيئًا قبل نجاح التحميل حتى لا نمسح البيانات القديمة
        self._dirty = False
        self._msg_id = None
        self._pinned = False

    @property
    def remote(self) -> bool:
        return bool(self.api and self.chat_id)

    # ------------------------------------------------------------- load / save

    def load(self) -> bool:
        try:
            loaded = self._read() or {}
        except Exception as e:
            print("storage: load failed:", e, flush=True)
            return False
        with self.lock:
            for k in SECTIONS:
                merged = dict(loaded.get(k) or {})
                merged.update(self.data[k])  # تغييرات حدثت قبل اكتمال التحميل
                self.data[k] = merged
            self.ready = True
        return True

    def _read(self):
        if self.remote:
            chat = self.api.call("getChat", chat_id=self.chat_id)
            pinned = chat.get("pinned_message") or {}
            doc = pinned.get("document") or {}
            if doc.get("file_name") != FILE_NAME:
                return None  # أول تشغيل: لا توجد نسخة بعد
            info = self.api.call("getFile", file_id=doc["file_id"])
            data = json.loads(self.api.download(info["file_path"]).decode("utf-8"))
            self._msg_id, self._pinned = pinned["message_id"], True
            return data
        if self.local_path and self.local_path.exists():
            return json.loads(self.local_path.read_text(encoding="utf-8"))
        return None

    def mark_dirty(self):
        with self.lock:
            self._dirty = True

    def flush(self) -> bool:
        with self._write_lock:
            with self.lock:
                if not self.ready or not self._dirty:
                    return True
                payload = json.dumps(self.data, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")
                self._dirty = False
            try:
                self._write(payload)
                return True
            except Exception as e:
                self.mark_dirty()
                print("storage: save failed:", e, flush=True)
                return False

    def _write(self, payload: bytes):
        if self.remote:
            file = (FILE_NAME, payload, "application/json")
            if self._msg_id:
                try:
                    self.api.call_files(
                        "editMessageMedia", {"file": file}, chat_id=self.chat_id,
                        message_id=self._msg_id,
                        media={"type": "document", "media": "attach://file", "caption": CAPTION})
                    if not self._pinned:
                        self._pin()
                    return
                except TelegramError as e:
                    if "not modified" in e.description:
                        return
                    print("storage: edit failed, sending a new copy:", e, flush=True)
            msg = self.api.call_files("sendDocument", {"document": file}, chat_id=self.chat_id,
                                      caption=CAPTION, disable_notification=True)
            self._msg_id, self._pinned = msg["message_id"], False
            self._pin()
        elif self.local_path:
            tmp = self.local_path.with_suffix(".tmp")
            tmp.write_bytes(payload)
            tmp.replace(self.local_path)

    def _pin(self):
        self.api.call("pinChatMessage", chat_id=self.chat_id, message_id=self._msg_id,
                      disable_notification=True)
        self._pinned = True

    def run_saver(self, interval: int = 10):
        while True:
            time.sleep(interval if self.ready else 30)
            if not self.ready:
                self.load()
            else:
                self.flush()

    # ------------------------------------------------------------------ data

    def get_sub(self, chat_id) -> dict | None:
        with self.lock:
            sub = self.data["subs"].get(str(chat_id))
            return dict(sub) if sub else None

    def set_sub(self, chat_id, sub: dict):
        with self.lock:
            self.data["subs"][str(chat_id)] = dict(sub)
            self._dirty = True

    def subs(self) -> dict[int, dict]:
        with self.lock:
            return {int(k): dict(v) for k, v in self.data["subs"].items()}

    def touch_user(self, chat_id):
        with self.lock:
            if str(chat_id) not in self.data["users"]:
                self.data["users"][str(chat_id)] = int(time.time())
                self._dirty = True

    def user_ids(self) -> list[int]:
        with self.lock:
            return [int(k) for k in self.data["users"]]

    def claim(self, key: str, ts: float) -> bool:
        """يعلّم مهمة (تنبيه معيّن) كمنفّذة. يرجع False إذا نُفذت من قبل."""
        with self.lock:
            if key in self.data["fired"]:
                return False
            self.data["fired"][key] = int(ts)
            self._dirty = True
            return True

    def has(self, key: str) -> bool:
        with self.lock:
            return key in self.data["fired"]

    def prune(self, before_ts: float):
        with self.lock:
            old = [k for k, t in self.data["fired"].items() if t < before_ts]
            for k in old:
                del self.data["fired"][k]
            if old:
                self._dirty = True
