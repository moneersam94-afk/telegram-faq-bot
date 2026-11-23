"""اختبارات بدون إنترنت: python -m unittest discover tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.core import (TelegramError, handle_update, load_faq, normalize,  # noqa: E402
                      search, tokens)


class FakeAPI:
    def __init__(self, fail_edit=None):
        self.calls = []
        self.fail_edit = fail_edit

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "editMessageText" and self.fail_edit:
            raise TelegramError(self.fail_edit)
        return True

    def last(self, method):
        return [p for m, p in self.calls if m == method][-1]


def text_msg(text, chat_type="private"):
    return {"update_id": 1, "message": {"chat": {"id": 42, "type": chat_type}, "text": text}}


def callback(data):
    return {"update_id": 2, "callback_query": {
        "id": "cb1", "data": data,
        "message": {"message_id": 7, "chat": {"id": 42, "type": "private"}}}}


def all_buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


class TestFaqFile(unittest.TestCase):
    def test_loads_and_callback_data_fits(self):
        faq = load_faq()
        for ci, cat in enumerate(faq["categories"]):
            for qi, _ in enumerate(cat["questions"]):
                self.assertLessEqual(len(f"q:{ci}:{qi}".encode()), 64)


class TestArabic(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("أوقاتُ الدوام!؟"), "اوقات الدوام")
        self.assertEqual(normalize("مدة إلى"), "مده الي")

    def test_tokens_strip_al_and_stopwords(self):
        self.assertEqual(tokens("ما هي أوقات الدوام"), {"اوقات", "دوام"})


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.faq = load_faq()

    def top(self, text):
        res = search(self.faq, text)
        return self.faq["categories"][res[0][1]]["questions"][res[0][2]]["q"] if res else None

    def test_matches_various_wordings(self):
        self.assertEqual(self.top("متى الدوام عندكم؟"), "ما هي أوقات العمل؟")
        self.assertEqual(self.top("وين موقعكم"), "أين تقع الفروع؟")
        self.assertEqual(self.top("بكم الخدمة"), "كم الأسعار؟")
        self.assertEqual(self.top("هل يوجد تقسيط"), "ما طرق الدفع المتاحة؟")
        self.assertEqual(self.top("متى يوصل الطلب"), "كم تستغرق مدة التنفيذ أو التوصيل؟")

    def test_no_match(self):
        self.assertEqual(search(self.faq, "مرحبا كيف حالك"), [])


class TestHandlers(unittest.TestCase):
    def setUp(self):
        self.faq = load_faq()

    def test_start_shows_menu_and_disclaimer(self):
        api = FakeAPI()
        handle_update(text_msg("/start"), api, self.faq)
        sent = api.last("sendMessage")
        self.assertIn("غير رسمي", sent["text"])
        self.assertNotIn("{company}", sent["text"])
        data = [b["callback_data"] for b in all_buttons(sent["reply_markup"])]
        self.assertEqual(data, ["c:0", "c:1", "c:2", "h"])

    def test_free_text_answer(self):
        api = FakeAPI()
        handle_update(text_msg("ما طرق الدفع؟"), api, self.faq)
        self.assertIn("طرق الدفع", api.last("sendMessage")["text"])

    def test_fallback(self):
        api = FakeAPI()
        handle_update(text_msg("سؤال غريب جدا"), api, self.faq)
        self.assertIn("لم أجد", api.last("sendMessage")["text"])

    def test_ignores_groups(self):
        api = FakeAPI()
        handle_update(text_msg("/start", chat_type="group"), api, self.faq)
        self.assertEqual(api.calls, [])

    def test_non_text_message(self):
        api = FakeAPI()
        upd = {"update_id": 3, "message": {"chat": {"id": 42, "type": "private"}, "photo": []}}
        handle_update(upd, api, self.faq)
        self.assertIn("النصية", api.last("sendMessage")["text"])

    def test_callback_flow(self):
        api = FakeAPI()
        handle_update(callback("c:1"), api, self.faq)
        self.assertEqual(api.calls[0][0], "answerCallbackQuery")
        edit = api.last("editMessageText")
        self.assertIn("الأسعار والدفع", edit["text"])

        handle_update(callback("q:1:0"), api, self.faq)
        self.assertIn("كم الأسعار", api.last("editMessageText")["text"])

        handle_update(callback("h"), api, self.faq)
        self.assertIn("@your_username", api.last("editMessageText")["text"])

        handle_update(callback("m"), api, self.faq)
        self.assertIn("أهلًا", api.last("editMessageText")["text"])

    def test_stale_button(self):
        api = FakeAPI()
        handle_update(callback("q:99:0"), api, self.faq)
        self.assertIn("لم يعد متاحًا", api.last("editMessageText")["text"])

    def test_edit_not_modified_is_silent(self):
        api = FakeAPI(fail_edit="editMessageText: message is not modified")
        handle_update(callback("m"), api, self.faq)
        self.assertFalse([m for m, _ in api.calls if m == "sendMessage"])

    def test_edit_failure_falls_back_to_send(self):
        api = FakeAPI(fail_edit="editMessageText: message to edit not found")
        handle_update(callback("m"), api, self.faq)
        self.assertTrue([m for m, _ in api.calls if m == "sendMessage"])


if __name__ == "__main__":
    unittest.main()
