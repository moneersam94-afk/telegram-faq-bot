"""أدوات المدير: الإحصائيات، الإرسال الجماعي، واستخراج روابط ومعرفات الفيديو للشروحات.

المدراء تُحدد معرفاتهم في متغير البيئة ADMIN_IDS (أي شخص يعرف معرّفه بالأمر /myid).
"""

from __future__ import annotations

import time

from bot.telegram import send_many
from bot.ui import Reply, btn, kb

HELP = """👑 أوامر المدير

/stats  إحصائيات المستخدمين ومشتركي المنبه
/broadcast  ردّ بهذا الأمر على أي رسالة (نص، صورة، فيديو) لإرسالها للجميع
/testalarm  تجربة شكل تنبيه المنبه

🎬 لإضافة فيديو للشروحات:
• أرسل الفيديو هنا، وسأعطيك معرّفه جاهزًا للنسخ
• أو حوّل لي منشورًا من قناتك، وسأعطيك رابطه"""

MEDIA_KINDS = ("video", "animation", "document", "photo", "audio", "voice")


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
                 "لإضافته في الشروحات ضع هذا الرابط داخل \"videos\" في content.json:\n"
                 f"{channel_post_link(chat, origin['message_id'])}\n\n"
                 "⚠️ يجب أن يكون البوت عضوًا (مشرفًا) في القناة حتى يقدر ينسخ المنشور.")
        return True
    for kind in MEDIA_KINDS:
        if kind in msg:
            obj = msg[kind][-1] if kind == "photo" else msg[kind]
            ref = obj["file_id"] if kind == "video" else f"{kind}:{obj['file_id']}"
            ctx.send("🎬 معرّف الملف جاهز. انسخه وضعه داخل \"videos\" في content.json:")
            ctx.send(ref)
            return True
    return False


def stats_text(app) -> str:
    store, now = app.store, time.time()
    on = [s for s in store.subs().values() if s.get("on")]
    paused = sum(1 for s in on if (s.get("pause") or 0) > now)
    lines = ["📊 إحصائيات البوت", "",
             f"👥 المستخدمون: {len(store.user_ids())}",
             f"⏰ مشتركو المنبه: {len(on)}",
             f"⏸ منهم متوقف مؤقتًا: {paused}"]
    for ev in app.alarm.events:
        n = sum(1 for s in on if ev["id"] in (s.get("ev") or []))
        lines.append(f"{ev.get('emoji', '')} {ev['name']}: {n}")
    lines += ["", "💾 التخزين: " + ("دائم على تلجرام ✅" if store.remote
                                    else "⚠️ مؤقت، يُمسح عند إعادة تشغيل السيرفر")]
    return "\n".join(lines)


def on_command(ctx, cmd: str, arg: str, msg: dict):
    """يرجع Reply للرد، أو None إذا انتهى بدون رد، أو False إذا ليس أمر مدير."""
    app = ctx.app
    if cmd == "/admin":
        return Reply(HELP)
    if cmd == "/stats":
        return Reply(stats_text(app))
    if cmd == "/testalarm":
        app.alarm.send_test(ctx.chat_id)
        return None
    if cmd == "/broadcast":
        target = msg.get("reply_to_message")
        if not target:
            return Reply("📣 للإرسال الجماعي: اكتب الرسالة (أو أرسل صورة أو فيديو)، "
                         "ثم ردّ عليها بالأمر /broadcast")
        subs = sum(1 for s in app.store.subs().values() if s.get("on"))
        users = len(app.store.user_ids())
        mid = target["message_id"]
        return Reply("📣 لمن أرسل هذه الرسالة؟", kb([
            [btn(f"⏰ مشتركو المنبه ({subs})", f"ad:bc:{mid}:s")],
            [btn(f"👥 كل المستخدمين ({users})", f"ad:bc:{mid}:a")],
            [btn("✖️ إلغاء", "ad:x")]]))
    return False


def on_callback(ctx, parts: list[str]) -> Reply:
    if not ctx.is_admin:
        raise ValueError("not an admin")
    app = ctx.app
    if parts[1] == "x":
        return Reply("✖️ تم الإلغاء.")
    if parts[1] == "bc":
        mid, audience = int(parts[2]), parts[3]
        if audience == "s":
            targets = [cid for cid, s in app.store.subs().items() if s.get("on")]
        else:
            targets = app.store.user_ids()
        src = ctx.chat_id

        def job():
            ok, failed = send_many(
                app.api, targets,
                lambda cid: ("copyMessage", {"from_chat_id": src, "message_id": mid}),
                on_blocked=app.alarm.mark_blocked)
            ctx.send(f"✅ انتهى الإرسال الجماعي.\nوصلت إلى: {ok}\nلم تصل: {failed}")

        app.run(job)
        return Reply(f"⏳ جارٍ الإرسال إلى {len(targets)}… سأخبرك عند الانتهاء.")
    raise ValueError(parts[1])
