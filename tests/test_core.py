"""اختبارات بدون إنترنت: python -m unittest discover tests"""

import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot.telegram  # noqa: E402
from bot.core import (App, handle_update, load_content, media_call, normalize,  # noqa: E402
                      search, tokens)
from bot.storage import FILE_NAME, Store  # noqa: E402
from bot.telegram import TelegramError  # noqa: E402

bot.telegram.SEND_DELAY = 0
UTC = timezone.utc
CONTENT = load_content()
ADMIN = 1000

FAQ_FIXTURE = [
    {"title": "الحساب", "questions": [
        {"q": "كيف أفتح حساب؟", "a": "جواب الحساب", "keywords": ["فتح حساب", "تسجيل", "حساب"]},
        {"q": "كيف أسحب أرباحي؟", "a": "جواب السحب", "keywords": ["سحب", "اسحب"]}]},
    {"title": "عام", "questions": [
        {"q": "ما هي أوقات العمل؟", "a": "جواب الأوقات", "keywords": ["اوقات", "دوام"]},
        {"q": "أين تقع الفروع؟", "a": "جواب الفروع", "keywords": ["موقع", "فرع"]}]},
]


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail = {}  # method -> function(params) قد ترفع خطأ
        self.chat = {}
        self.blob = b""
        self._id = 100

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in self.fail:
            self.fail[method](params)
        if method == "getChat":
            return self.chat
        if method == "getFile":
            return {"file_path": "docs/1.json"}
        self._id += 1
        return {"message_id": self._id}

    def call_files(self, method, files, **params):
        self.calls.append((method, params))
        self.uploaded = files
        return {"message_id": 555}

    def download(self, path):
        return self.blob

    def of(self, method):
        return [p for m, p in self.calls if m == method]

    def last(self, method):
        return self.of(method)[-1]


def make_app(content=None):
    store = Store()
    store.load()
    return App(FakeAPI(), content or copy.deepcopy(CONTENT), store, [ADMIN], run=lambda f: f())


def msg(text=None, chat_type="private", uid=42, **extra):
    m = {"message_id": 5, "chat": {"id": uid, "type": chat_type},
         "from": {"id": uid, "first_name": "أحمد"}, **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": 1, "message": m}


def cb(data, uid=42, text="قائمة"):
    return {"update_id": 2, "callback_query": {
        "id": "cb1", "data": data, "from": {"id": uid, "first_name": "أحمد"},
        "message": {"message_id": 7, "chat": {"id": uid, "type": "private"}, "text": text}}}


def buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


def shown(api):
    """آخر نص ظهر للمستخدم (تعديل أو رسالة جديدة)."""
    return [p for m, p in api.calls if m in ("editMessageText", "sendMessage")][-1]


class TestContent(unittest.TestCase):
    def test_loads_and_all_buttons_fit(self):
        app = make_app()
        for data in ["m", "e", "f", "r", "a", "a:on", "a:tz:0:1", "a:ld", "a:ps", "a:tz",
                     "a:ofp:1"] + [f"r:{i}" for i in range(len(CONTENT["report"]["types"]))]:
            handle_update(cb(data), app)
            for b in buttons(shown(app.api).get("reply_markup") or {"inline_keyboard": []}):
                self.assertLessEqual(len(b.get("callback_data", "").encode()), 64)

    def test_bad_alarm_time_rejected(self):
        c = copy.deepcopy(CONTENT)
        c["alarm"]["events"][0]["time_gmt"] = "25:00"
        with self.assertRaises(ValueError):
            make_app(c)


class TestArabicSearch(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("أوقاتُ الدوام!؟"), "اوقات الدوام")
        self.assertEqual(tokens("ما هي أوقات الدوام"), {"اوقات", "دوام"})

    def top(self, text):
        res = search(FAQ_FIXTURE, text)
        return FAQ_FIXTURE[res[0][1]]["questions"][res[0][2]]["q"] if res else None

    def test_matches_various_wordings(self):
        self.assertEqual(self.top("متى الدوام عندكم؟"), "ما هي أوقات العمل؟")
        self.assertEqual(self.top("وين موقعكم"), "أين تقع الفروع؟")
        self.assertEqual(self.top("بدي اسحب الارباح"), "كيف أسحب أرباحي؟")
        self.assertEqual(search(FAQ_FIXTURE, "مرحبا كيف حالك"), [])


class TestMenus(unittest.TestCase):
    def test_start(self):
        app = make_app()
        handle_update(msg("/start"), app)
        sent = app.api.last("sendMessage")
        self.assertIn("أحمد", sent["text"])
        self.assertIn("ويلث إكسل", sent["text"])
        self.assertEqual([b["callback_data"] for b in buttons(sent["reply_markup"])],
                         ["e", "f", "r", "a"])
        self.assertIn("تفعيل", buttons(sent["reply_markup"])[3]["text"])

    def test_ignores_groups(self):
        app = make_app()
        handle_update(msg("/start", chat_type="group"), app)
        self.assertEqual(app.api.calls, [])

    def test_empty_sections_say_soon(self):
        c = copy.deepcopy(CONTENT)
        c["explanations"]["topics"] = []
        app = make_app(c)
        handle_update(cb("e"), app)
        self.assertIn("قريبًا", app.api.last("editMessageText")["text"])
        handle_update(cb("f"), app)
        self.assertIn("قريبًا", app.api.last("editMessageText")["text"])

    def test_faq_flow_and_free_text(self):
        c = copy.deepcopy(CONTENT)
        c["faq"]["categories"] = FAQ_FIXTURE
        app = make_app(c)
        handle_update(cb("c:0"), app)
        self.assertIn("الحساب", app.api.last("editMessageText")["text"])
        handle_update(cb("q:0:1"), app)
        self.assertIn("جواب السحب", app.api.last("editMessageText")["text"])
        handle_update(msg("وين الفرع"), app)
        self.assertIn("جواب الفروع", app.api.last("sendMessage")["text"])

    def test_intents_and_fallback(self):
        app = make_app()
        handle_update(msg("عندي مشكلة بالسحب"), app)
        self.assertIn("نوع المشكلة", app.api.last("sendMessage")["text"])
        handle_update(msg("متى موعد الصفقة"), app)
        self.assertIn("منبه الصفقات", app.api.last("sendMessage")["text"])
        handle_update(msg("سؤال غريب جدا"), app)
        self.assertIn("لم أجد", app.api.last("sendMessage")["text"])

    def test_stale_button_and_edit_errors(self):
        app = make_app()
        handle_update(cb("q:99:0"), app)
        self.assertIn("لم يعد متاحًا", app.api.last("editMessageText")["text"])

        def not_modified(p):
            raise TelegramError("editMessageText", "Bad Request: message is not modified")
        app.api.fail["editMessageText"] = not_modified
        handle_update(cb("m"), app)
        self.assertFalse(app.api.of("sendMessage"))

        def gone(p):
            raise TelegramError("editMessageText", "Bad Request: message to edit not found")
        app.api.fail["editMessageText"] = gone
        handle_update(cb("m"), app)
        self.assertTrue(app.api.of("sendMessage"))


class TestExplanations(unittest.TestCase):
    def setUp(self):
        c = copy.deepcopy(CONTENT)
        c["explanations"]["topics"] = [
            {"title": "نص فقط", "text": "الشرح"},
            {"title": "من القناة", "posts": ["https://t.me/c/123456/7"]},
            {"title": "عدة ملفات", "text": "شرح", "videos": ["BAACAgQ", "document:XYZ"],
             "buttons": [{"text": "يوتيوب", "url": "https://youtube.com/x"}]},
        ]
        self.app = make_app(c)

    def test_text_topic(self):
        handle_update(cb("e:0"), self.app)
        self.assertIn("الشرح", self.app.api.last("editMessageText")["text"])

    def test_channel_video_is_copied_with_buttons(self):
        handle_update(cb("e:1"), self.app)
        p = self.app.api.last("copyMessage")
        self.assertEqual((p["from_chat_id"], p["message_id"]), (-100123456, 7))
        self.assertNotIn("caption", p)  # يبقى وصف المنشور الأصلي
        self.assertIn("reply_markup", p)

    def test_many_files(self):
        handle_update(cb("e:2"), self.app)
        self.assertEqual(self.app.api.last("sendVideo")["video"], "BAACAgQ")
        doc = self.app.api.last("sendDocument")
        self.assertEqual(doc["document"], "XYZ")
        self.assertIn("youtube", json.dumps(doc["reply_markup"]))

    def test_media_call_public_link(self):
        self.assertEqual(media_call("t.me/mychannel/5"),
                         ("copyMessage", {"from_chat_id": "@mychannel", "message_id": 5}))


class TestReport(unittest.TestCase):
    def test_contact_button(self):
        c = copy.deepcopy(CONTENT)
        c["report"]["types"] = [{"title": "سحب", "contact": "@support_team"},
                                {"title": "أخرى", "contact": ""}]
        app = make_app(c)
        handle_update(cb("r:0"), app)
        urls = [b.get("url") for b in buttons(app.api.last("editMessageText")["reply_markup"])]
        self.assertIn("https://t.me/support_team", urls)
        handle_update(cb("r:1"), app)
        edit = app.api.last("editMessageText")
        self.assertIn("قريبًا", edit["text"])
        self.assertFalse([b for b in buttons(edit["reply_markup"]) if "url" in b])


class TestAlarmSettings(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.sub = lambda: self.app.store.get_sub(42)

    def test_activation_asks_country_first(self):
        handle_update(cb("a"), self.app)
        self.assertIn("a:on", json.dumps(self.app.api.last("editMessageText")))
        handle_update(cb("a:on"), self.app)
        self.assertIn("اختر بلدك", self.app.api.last("editMessageText")["text"])
        handle_update(cb("a:tz:0:1"), self.app)
        self.assertEqual((self.sub()["on"], self.sub()["tz"]), (True, "Asia/Riyadh"))
        self.assertIn("تفعيل", self.app.api.last("answerCallbackQuery")["text"])
        panel = self.app.api.last("editMessageText")["text"]
        self.assertIn("6:30 م – 8:30 م بتوقيتك", panel)  # 15:30 GMT = 6:30 م السعودية، لساعتين
        self.assertIn("9:30 م – 11:30 م بتوقيتك", panel)
        handle_update(msg("/start"), self.app)
        self.assertIn("مفعّل", buttons(self.app.api.last("sendMessage")["reply_markup"])[3]["text"])

    def test_settings(self):
        handle_update(cb("a:tz:0:1"), self.app)
        handle_update(cb("a:ev:0"), self.app)
        self.assertEqual(self.sub()["ev"], ["profit"])
        handle_update(cb("a:ev:1"), self.app)  # آخر تنبيه لا يُلغى
        self.assertEqual(self.sub()["ev"], ["profit"])
        self.assertTrue(self.app.api.last("answerCallbackQuery")["show_alert"])
        handle_update(cb("a:ev:0"), self.app)
        self.assertEqual(self.sub()["ev"], ["trade", "profit"])
        handle_update(cb("a:ld:30"), self.app)
        self.assertEqual(self.sub()["lead"], 30)
        handle_update(cb("a:of:-300:0"), self.app)
        self.assertEqual(self.sub()["tz"], "-300")
        self.assertIn("10:30 ص – 12:30 م بتوقيتك", self.app.api.last("editMessageText")["text"])
        handle_update(cb("a:ps:1"), self.app)
        self.assertGreater(self.sub()["pause"], datetime.now(UTC).timestamp())
        self.assertIn("متوقف مؤقتًا", self.app.api.last("editMessageText")["text"])
        handle_update(cb("a:rs"), self.app)
        self.assertEqual(self.sub()["pause"], 0)
        handle_update(cb("a:off"), self.app)
        self.assertFalse(self.sub()["on"])

    def test_test_alert_and_ack(self):
        handle_update(cb("a:t"), self.app)
        sent = self.app.api.last("sendMessage")
        self.assertIn("تجريبي", sent["text"])
        self.assertIn("a:sz:0", json.dumps(sent["reply_markup"]))
        handle_update(cb("a:ok", text="🔔 حان الآن"), self.app)
        edit = self.app.api.last("editMessageText")
        self.assertIn("✅ تمّ", edit["text"])
        self.assertNotIn("reply_markup", edit)


class TestScheduler(unittest.TestCase):
    T0 = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)  # يوم ثلاثاء

    def setUp(self):
        self.app = make_app()
        self.alarm = self.app.alarm
        self.app.store.set_sub(42, {"on": True, "ev": ["trade", "profit"], "lead": 10,
                                    "tz": "Asia/Riyadh", "pause": 0})

    def texts(self):
        return [(p["chat_id"], p["text"]) for p in self.app.api.of("sendMessage")]

    def test_pre_alert_then_main_without_duplicates(self):
        self.alarm.tick(self.T0 - timedelta(minutes=10))
        self.alarm.tick(self.T0 - timedelta(minutes=9, seconds=50))
        texts = self.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("يبدأ بعد 10 دقائق", texts[0][1])
        self.assertIn("6:30 م – 8:30 م بتوقيتك", texts[0][1])

        self.alarm.tick(self.T0)
        self.alarm.tick(self.T0 + timedelta(seconds=20))
        self.assertEqual(len(self.texts()), 2)
        main = self.app.api.last("sendMessage")
        self.assertIn("موعد الصفقة متاح الآن", main["text"])
        self.assertIn("a:ok", json.dumps(main["reply_markup"]))

    def test_skips_paused_off_and_other_days(self):
        self.app.store.set_sub(43, {"on": False, "ev": ["trade"], "lead": 0, "tz": None})
        self.app.store.set_sub(44, {"on": True, "ev": ["trade"], "lead": 0, "tz": None,
                                    "pause": self.T0.timestamp() + 3600})
        self.app.store.set_sub(45, {"on": True, "ev": ["profit"], "lead": 0, "tz": None})
        self.alarm.tick(self.T0)
        self.assertEqual([cid for cid, _ in self.texts()], [42])

        app2 = make_app()
        app2.store.set_sub(42, {"on": True, "ev": ["trade"], "lead": 0, "tz": None})
        app2.alarm.days = {0, 1, 2, 3, 4}  # من الاثنين إلى الجمعة
        app2.alarm.tick(datetime(2026, 11, 28, 15, 30, tzinfo=UTC))  # سبت
        self.assertFalse(app2.api.of("sendMessage"))

    def test_late_server_still_alerts_while_window_open(self):
        self.alarm.tick(self.T0 + timedelta(minutes=11))
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("يُغلق بعد ساعة و49 دقيقة", self.texts()[0][1])

        app2 = make_app()
        app2.store.set_sub(42, {"on": True, "ev": ["trade"], "lead": 0, "tz": None})
        app2.alarm.tick(self.T0 + timedelta(minutes=120))  # أُغلق الموعد
        self.assertFalse(app2.api.of("sendMessage"))

    def test_closing_reminder_skips_who_pressed_done(self):
        self.app.store.set_sub(46, {"on": True, "ev": ["trade"], "lead": 0, "tz": None})
        handle_update(cb("a:ok:0:20261124", uid=46, text="🔔"), self.app)
        self.alarm.tick(self.T0 + timedelta(minutes=105))  # قبل الإغلاق بربع ساعة
        closing = [(c, x) for c, x in self.texts() if "تنبيه أخير" in x]
        self.assertEqual([c for c, _ in closing], [42])
        self.assertIn("يُغلق بعد 15 دقيقة", closing[0][1])
        self.assertIn("8:30 م", closing[0][1])

    def test_blocked_user_is_unsubscribed(self):
        def blocked(p):
            raise TelegramError("sendMessage", "Forbidden: bot was blocked by the user", 403)
        self.app.api.fail["sendMessage"] = blocked
        self.alarm.tick(self.T0)
        self.assertFalse(self.app.store.get_sub(42)["on"])

    def test_snooze(self):
        self.alarm._now = lambda: self.T0 + timedelta(minutes=10)
        handle_update(cb("a:sz:0", text="🔔 موعد الصفقة متاح الآن"), self.app)
        self.assertIn("سأذكّرك", self.app.api.last("editMessageText")["text"])
        self.alarm.tick(self.T0 + timedelta(minutes=16))
        self.assertIn("تذكير: 📈 موعد الصفقة", self.app.api.last("sendMessage")["text"])

    def test_snooze_refused_when_window_closing_or_closed(self):
        self.alarm._now = lambda: self.T0 + timedelta(minutes=117)
        handle_update(cb("a:sz:0"), self.app)
        self.assertIn("يُغلق قبل ذلك", self.app.api.last("answerCallbackQuery")["text"])
        self.alarm._now = lambda: self.T0 + timedelta(minutes=130)
        handle_update(cb("a:sz:0"), self.app)
        self.assertIn("انتهى وقت", self.app.api.last("answerCallbackQuery")["text"])


class TestStorage(unittest.TestCase):
    def test_saves_to_pinned_document_and_loads_back(self):
        api = FakeAPI()
        store = Store(api=api, chat_id=ADMIN)
        self.assertTrue(store.load())
        store.set_sub(42, {"on": True})
        store.flush()
        self.assertEqual(api.of("sendDocument")[0]["chat_id"], ADMIN)
        self.assertEqual(api.last("pinChatMessage")["message_id"], 555)
        store.set_sub(43, {"on": False})
        store.flush()
        self.assertTrue(api.of("editMessageMedia"))
        blob = api.uploaded["file"][1]

        api2 = FakeAPI()
        api2.chat = {"pinned_message": {"message_id": 555, "document": {
            "file_name": FILE_NAME, "file_id": "F1"}}}
        api2.blob = blob
        store2 = Store(api=api2, chat_id=ADMIN)
        store2.load()
        self.assertEqual(set(store2.subs()), {42, 43})

    def test_no_save_when_load_failed(self):
        api = FakeAPI()

        def down(p):
            raise OSError("network down")
        api.fail["getChat"] = down
        store = Store(api=api, chat_id=ADMIN)
        self.assertFalse(store.load())
        store.set_sub(42, {"on": True})
        store.flush()
        self.assertFalse(api.of("sendDocument"))  # لا نكتب فوق البيانات القديمة


class TestAdmin(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def test_myid_for_everyone_stats_for_admin_only(self):
        handle_update(msg("/myid"), self.app)
        self.assertIn("42", self.app.api.last("sendMessage")["text"])
        handle_update(msg("/stats"), self.app)
        self.assertNotIn("إحصائيات", self.app.api.last("sendMessage")["text"])
        handle_update(msg("/stats", uid=ADMIN), self.app)
        self.assertIn("إحصائيات", self.app.api.last("sendMessage")["text"])

    def test_video_and_forwarded_post_give_ids(self):
        handle_update(msg(uid=ADMIN, video={"file_id": "VID123"}), self.app)
        self.assertEqual(self.app.api.last("sendMessage")["text"], "VID123")
        handle_update(msg(uid=ADMIN, forward_origin={
            "type": "channel", "message_id": 9, "chat": {"id": -100555, "title": "قناتي"}}),
            self.app)
        self.assertIn("https://t.me/c/555/9", self.app.api.last("sendMessage")["text"])
        handle_update(msg(video={"file_id": "X"}), self.app)  # غير المدير
        self.assertIn("النصية", self.app.api.last("sendMessage")["text"])

    def test_broadcast(self):
        handle_update(msg("مرحبا"), self.app)  # مستخدم 42
        handle_update(msg("/broadcast", uid=ADMIN, reply_to_message={"message_id": 77}), self.app)
        self.assertIn("ad:bc:77:a", json.dumps(self.app.api.last("sendMessage")))
        handle_update(cb("ad:bc:77:a", uid=42), self.app)  # غير المدير لا يقدر
        self.assertFalse(self.app.api.of("copyMessage"))
        handle_update(cb("ad:bc:77:a", uid=ADMIN), self.app)
        targets = {p["chat_id"] for p in self.app.api.of("copyMessage")}
        self.assertEqual(targets, {42, ADMIN})
        self.assertIn("انتهى الإرسال", self.app.api.last("sendMessage")["text"])

    def test_channel_admin_announces_id(self):
        handle_update({"update_id": 9, "my_chat_member": {
            "chat": {"id": -100777, "type": "channel"},
            "new_chat_member": {"status": "administrator"}}}, self.app)
        self.assertIn("-100777", self.app.api.last("sendMessage")["text"])


if __name__ == "__main__":
    unittest.main()
