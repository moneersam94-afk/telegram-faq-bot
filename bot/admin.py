"""أدوات المدير: الإحصائيات، الإرسال الجماعي، القروبات، مراسلة مستخدم معيّن،
واستخراج روابط ومعرفات الفيديو للشروحات.

المدراء تُحدد معرفاتهم في متغير البيئة ADMIN_IDS (أي شخص يعرف معرّفه بالأمر /myid).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from bot.telegram import TelegramError, send_many
from bot.ui import Reply, btn, kb

HELP = """👑 أوامر المدير

/stats  إحصائيات المستخدمين والمشتركين والقروبات
/groups  إدارة القروبات وتنبيهاتها
/broadcast  ردّ بهذا الأمر على أي رسالة (نص، صورة، فيديو) لإرسالها جماعيًا
/send @username  مراسلة مستخدم معيّن (أو بمعرّفه الرقمي)
/user اسم  البحث عن مستخدم بالاسم أو اليوزر
/testalarm  تجربة شكل تنبيه المنبه
/cancel  إلغاء أي عملية جارية

🎬 لإضافة فيديو للشروحات:
• أرسل الفيديو هنا، وسأعطيك معرّفه جاهزًا للنسخ
• أو حوّل لي منشورًا من قناتك، وسأعطيك رابطه"""

MEDIA_KINDS = ("video", "animation", "document", "photo", "audio", "voice")
AUDIENCES = {"s": "مشتركي المنبه", "a": "كل المستخدمين", "g": "القروبات",
             "x": "الكل (المستخدمين والقروبات)"}
REPLY_BUTTON = {"inline_keyboard": [[{"text": "↩️ الرد على الإدارة", "callback_data": "rp"}]]}


def channel_post_link(chat: dict, message_id: int) -> str:
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    return f"https://t.me/c/{str(chat['id']).removeprefix('-100')}/{message_id}"


def on_media(ctx, msg: dict) -> bool:
    """يرد على المدير بمعرف أي ملف أو رابط منشور يرسله. يرجع True إذا تعامل مع الرسالة."""
    origin = msg.get("forward_origin") or {}
    if origin.get("type") == "channel" and origin.get("message_id"):
        chat = origin["chat"]
        ctx.send(f"📌 منشور من قناة: {chat.get('title', '')}\n🆔 معرف القناة: {chat['id']}\n\n"
                 "لإضافته في الشروحات ضع هذا الرابط داخل \"posts\" في content.json:\n"
                 f"{channel_post_link(chat, origin['message_id'])}\n\n"
                 "⚠️ يجب أن يكون البوت مشرفًا في القناة حتى يقدر ينسخ المنشور.")
        return True
    for kind in MEDIA_KINDS:
        if kind in msg:
            obj = msg[kind][-1] if kind == "photo" else msg[kind]
            ref = obj["file_id"] if kind == "video" else f"{kind}:{obj['file_id']}"
            ctx.send("🎬 معرّف الملف جاهز. انسخه وضعه داخل \"posts\" في content.json:")
            ctx.send(ref)
            return True
    return False


def stats_text(app) -> str:
    store, now = app.store, time.time()
    on = [s for s in store.subs().values() if s.get("on")]
    paused = sum(1 for s in on if (s.get("pause") or 0) > now)
    groups = store.groups()
    lines = ["📊 إحصائيات البوت", "",
             f"👥 المستخدمون: {len(store.user_ids())}",
             f"⏰ مشتركو المنبه: {len(on)}",
             f"⏸ منهم متوقف مؤقتًا: {paused}"]
    for ev in app.alarm.events:
        n = sum(1 for s in on if ev["id"] in (s.get("ev") or []))
        lines.append(f"{ev.get('emoji', '')} {ev['name']}: {n}")
    lines += [f"💬 القروبات: {len(groups)} (المفعّلة: "
              f"{sum(1 for g in groups.values() if g.get('on', True))})"]
    lines += ["", "💾 التخزين: " + ("دائم على تلجرام ✅" if store.remote
                                    else "⚠️ مؤقت، يُمسح عند إعادة تشغيل السيرفر")]
    return "\n".join(lines)


# ------------------------------------------------------------ users / send


def display_name(info: dict | None, uid) -> str:
    info = info or {}
    name = info.get("n") or "بدون اسم"
    return f"{name} (@{info['u']})" if info.get("u") else f"{name} ({uid})"


def user_card(app, uid: int, info: dict | None) -> str:
    info = info or {}
    lines = [f"👤 {info.get('n') or 'بدون اسم'}"]
    if info.get("u"):
        lines.append(f"🔗 @{info['u']}")
    lines.append(f"🆔 {uid}")
    lines.append(f"⏰ مشترك بالمنبه: {'✅' if app.alarm.is_on(uid) else '❌'}")
    if info.get("t"):
        first = datetime.fromtimestamp(info["t"], timezone.utc)
        lines.append(f"📅 أول استخدام: {first:%Y-%m-%d}")
    return "\n".join(lines)


def resolve_user(app, arg: str) -> tuple[int, dict] | None:
    arg = re.sub(r"^(https?://)?t\.me/", "", arg.strip()).lstrip("@")
    if arg.lstrip("-").isdigit():
        info = app.store.get_user(int(arg))
        return (int(arg), info) if info is not None else None
    for uid, info in app.store.find_users(arg):
        if (info.get("u") or "").lower() == arg.lower():
            return uid, info
    return None


def start_send(ctx, uid: int) -> Reply:
    info = ctx.app.store.get_user(uid)
    ctx.app.pending[ctx.chat_id] = {"act": "send", "to": uid}
    return Reply(user_card(ctx.app, uid, info) + "\n\n✉️ أرسل الآن الرسالة التي تريد إيصالها له "
                 "(نص أو صورة أو فيديو).\nللإلغاء: /cancel")


def confirm_send(ctx, msg: dict, uid: int):
    """بعد أن يكتب المدير رسالته: نطلب التأكيد قبل الإرسال."""
    name = display_name(ctx.app.store.get_user(uid), uid)
    ctx.send(f"📤 أرسل هذه الرسالة إلى {name}؟",
             kb([[btn("✅ إرسال", f"ad:sd:{msg['message_id']}:{uid}"), btn("✖️ إلغاء", "ad:x")]]),
             reply_parameters={"message_id": msg["message_id"]})


def forward_reply(ctx, msg: dict):
    """رد المستخدم على رسالة الإدارة: يصل لكل المدراء مع زر للرد عليه."""
    app, uid = ctx.app, ctx.chat_id
    name = display_name(app.store.get_user(uid), uid)
    for admin_id in app.admins:
        try:
            app.api.call("sendMessage", chat_id=admin_id, text=f"📩 رد من {name}\n🆔 {uid}",
                         reply_markup=kb([[btn("↩️ رد عليه", f"ad:sn:{uid}")]]))
            app.api.call("copyMessage", chat_id=admin_id, from_chat_id=uid,
                         message_id=msg["message_id"])
        except TelegramError as e:
            print("forward reply failed:", e, flush=True)
    ctx.send("✅ وصل ردك للإدارة، شكرًا لك.")


# ---------------------------------------------------------------- commands


def on_command(ctx, cmd: str, arg: str, msg: dict):
    """يرجع Reply للرد، أو None إذا انتهى بدون رد، أو False إذا ليس أمر مدير."""
    app = ctx.app
    if cmd == "/admin":
        return Reply(HELP)
    if cmd == "/stats":
        return Reply(stats_text(app))
    if cmd == "/groups":
        return app.groups.menu()
    if cmd == "/testalarm":
        app.alarm.send_test(ctx.chat_id)
        return None
    if cmd == "/send":
        if not arg:
            return Reply("✉️ لمراسلة مستخدم: /send @username أو /send ومعرّفه الرقمي.\n"
                         "لا تعرف اليوزر؟ ابحث بالاسم: /user احمد")
        found = resolve_user(app, arg)
        if not found:
            return Reply("❌ لم أجد هذا المستخدم.\nيجب أن يكون قد بدأ البوت من قبل. "
                         "جرّب البحث بالاسم: /user الاسم")
        return start_send(ctx, found[0])
    if cmd == "/user":
        if not arg:
            return Reply("🔎 اكتب الاسم أو اليوزر بعد الأمر، مثل: /user احمد")
        results = app.store.find_users(arg)
        if not results:
            return Reply("❌ لا يوجد مستخدم بهذا الاسم أو اليوزر.")
        rows = [[btn("👤 " + display_name(info, uid)[:50], f"ad:us:{uid}")] for uid, info in results]
        return Reply(f"🔎 نتائج البحث ({len(results)}):", kb(rows))
    if cmd == "/broadcast":
        target = msg.get("reply_to_message")
        if not target:
            return Reply("📣 للإرسال الجماعي: اكتب الرسالة (أو أرسل صورة أو فيديو)، "
                         "ثم ردّ عليها بالأمر /broadcast")
        subs = sum(1 for s in app.store.subs().values() if s.get("on"))
        users = len(app.store.user_ids())
        groups = len(app.store.groups())
        mid = target["message_id"]
        return Reply("📣 لمن أرسل هذه الرسالة؟", kb([
            [btn(f"⏰ مشتركو المنبه ({subs})", f"ad:bc:{mid}:s")],
            [btn(f"👥 كل المستخدمين ({users})", f"ad:bc:{mid}:a")],
            [btn(f"💬 القروبات ({groups})", f"ad:bc:{mid}:g")],
            [btn(f"🌐 الكل ({users + groups})", f"ad:bc:{mid}:x")],
            [btn("✖️ إلغاء", "ad:x")]]))
    return False


def _targets(app, audience: str) -> list[int]:
    if audience == "s":
        return [cid for cid, s in app.store.subs().items() if s.get("on")]
    users = app.store.user_ids()
    groups = list(app.store.groups())
    return {"a": users, "g": groups, "x": users + groups}[audience]


def on_callback(ctx, parts: list[str]) -> Reply:
    if not ctx.is_admin:
        raise ValueError("not an admin")
    app, act = ctx.app, parts[1]
    if act == "x":
        app.pending.pop(ctx.chat_id, None)
        return Reply("✖️ تم الإلغاء.")
    if act == "bc":
        mid, audience = int(parts[2]), parts[3]
        targets = _targets(app, audience)
        src = ctx.chat_id

        def lost(cid):
            app.groups.lost(cid) if cid < 0 else app.alarm.mark_blocked(cid)

        def job():
            ok, failed = send_many(
                app.api, targets,
                lambda cid: ("copyMessage", {"from_chat_id": src, "message_id": mid}),
                on_blocked=lost)
            ctx.send(f"✅ انتهى الإرسال الجماعي إلى {AUDIENCES[audience]}.\n"
                     f"وصلت إلى: {ok}\nلم تصل: {failed}")

        app.run(job)
        return Reply(f"⏳ جارٍ الإرسال إلى {AUDIENCES[audience]} ({len(targets)})… "
                     "سأخبرك عند الانتهاء.")
    if act == "us":
        uid = int(parts[2])
        return Reply(user_card(app, uid, app.store.get_user(uid)),
                     kb([[btn("✉️ مراسلته", f"ad:sn:{uid}")]]))
    if act == "sn":
        reply = start_send(ctx, int(parts[2]))
        ctx.send(reply.text)
        return Reply(toast="✉️ اكتب رسالتك")
    if act == "sd":
        mid, uid = int(parts[2]), int(parts[3])
        name = display_name(app.store.get_user(uid), uid)
        try:
            app.api.call("copyMessage", chat_id=uid, from_chat_id=ctx.chat_id, message_id=mid,
                         reply_markup=REPLY_BUTTON)
        except TelegramError as e:
            if e.blocked:
                app.alarm.mark_blocked(uid)
                return Reply(f"❌ لم تصل الرسالة إلى {name}: حظر البوت أو حذف حسابه.")
            return Reply(f"❌ لم تصل الرسالة إلى {name}: {e.description}")
        return Reply(f"✅ تم إرسال الرسالة إلى {name}.\nإذا رد عليك سيصلك رده هنا.")
    raise ValueError(act)
