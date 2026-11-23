"""الاتصال بـ Telegram Bot API باستخدام مكتبة بايثون القياسية فقط."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

API_BASE = "https://api.telegram.org"
SEND_DELAY = 0.05  # بين رسائل الإرسال الجماعي (حد تلجرام ≈ 30 رسالة في الثانية)


class TelegramError(Exception):
    def __init__(self, method: str, description: str | None, code: int | None = None,
                 retry_after: int | None = None):
        super().__init__(f"{method}: {description}")
        self.description = description or ""
        self.code = code
        self.retry_after = retry_after

    @property
    def blocked(self) -> bool:
        """المستخدم حظر البوت أو حذف حسابه، فلا فائدة من الإرسال له مجددًا."""
        d = self.description.lower()
        return self.code == 403 or "chat not found" in d or "deactivated" in d


class TelegramAPI:
    def __init__(self, token: str, timeout: int = 40):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN غير موجود")
        self._token = token
        self.timeout = timeout

    def call(self, method: str, **params):
        return self._post(method, json.dumps(params).encode("utf-8"), "application/json")

    def call_files(self, method: str, files: dict, **params):
        """إرسال ملفات. files = {"اسم الحقل": (اسم الملف, bytes, نوع المحتوى)}"""
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in params.items():
            if not isinstance(value, str):
                value = json.dumps(value)
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8"))
        for key, (filename, data, ctype) in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode("utf-8")
                + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        return self._post(method, b"".join(parts), f"multipart/form-data; boundary={boundary}")

    def download(self, file_path: str) -> bytes:
        url = f"{API_BASE}/file/bot{self._token}/{file_path}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            return resp.read()

    def _post(self, method: str, body: bytes, ctype: str):
        req = urllib.request.Request(f"{API_BASE}/bot{self._token}/{method}", data=body,
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                result = json.loads(e.read().decode("utf-8"))
            except Exception:
                raise TelegramError(method, f"HTTP {e.code}", e.code) from None
        if not result.get("ok"):
            raise TelegramError(method, result.get("description"), result.get("error_code"),
                                (result.get("parameters") or {}).get("retry_after"))
        return result.get("result")


def send_many(api, chat_ids, make, on_blocked=None, delay: float | None = None,
              sleep=time.sleep) -> tuple[int, int]:
    """يرسل لعدة محادثات مع احترام حدود تلجرام.

    make(chat_id) يرجع (method, params) أو None للتخطي.
    on_blocked(chat_id) يُستدعى لمن حظر البوت. يرجع (عدد الناجح، عدد الفاشل).
    """
    delay = SEND_DELAY if delay is None else delay
    ok = failed = 0
    for cid in chat_ids:
        job = make(cid)
        if not job:
            continue
        method, params = job
        for attempt in range(3):
            try:
                api.call(method, chat_id=cid, **params)
                ok += 1
                break
            except TelegramError as e:
                if e.retry_after and attempt < 2:
                    sleep(e.retry_after + 1)
                    continue
                if e.blocked and on_blocked:
                    on_blocked(cid)
            except OSError:
                if attempt < 2:
                    sleep(2)
                    continue
            failed += 1
            break
        if delay:
            sleep(delay)
    return ok, failed
